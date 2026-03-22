#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据获取模块 - 基于 Tushare Pro + SQLite 缓存
首次获取后存入本地数据库，后续增量补数据，大幅减少 API 调用
"""
import time
import logging
import os
import random
import threading
import tushare as ts
import pandas as pd
from datetime import datetime, timedelta

from BBBIG.config import TUSHARE_TOKEN
from BBBIG.db_cache import db_cache

logger = logging.getLogger("BBBIG")

# 全局 Tushare API 并发信号量：串行化所有 Tushare 调用（快速请求会触发"IP超限"限流）
_tushare_semaphore = threading.Semaphore(1)


class StockDataFetcher:
    """A股数据获取器 (Tushare Pro + SQLite 缓存)"""

    def __init__(self):
        ts.set_token(TUSHARE_TOKEN)
        self.pro = ts.pro_api()
        self._api_interval = 2.0  # API 调用间隔（秒），低于2s会触发Tushare "IP超限"限流

    def _api_sleep(self):
        """API 调用间隔，避免限流"""
        time.sleep(self._api_interval)

    def _call_api(self, fn, *args, max_retries=3, **kwargs):
        """带并发控制的 Tushare API 调用（串行 + 限流自动退避重试）

        限流机制（实测）：Tushare 用滑动窗口限流，连续快速请求 ~5 次后触发
        "IP数量超限"惩罚，惩罚期约 60s。因此：
          - 正常调用间隔 2s（_api_interval）
          - 触发限流后等 60s 再重试
        """
        for attempt in range(max_retries):
            with _tushare_semaphore:
                try:
                    result = fn(*args, **kwargs)
                    self._api_sleep()
                    return result
                except Exception as e:
                    err = str(e)
                    if "IP数量超限" in err or ("IP" in err and "超限" in err):
                        wait = 60  # 实测惩罚期 ~60s
                        logger.warning(f"Tushare 限流（IP超限），等待 {wait}s 冷却后重试（{attempt+1}/{max_retries}）...")
                        time.sleep(wait)
                    elif "每分钟" in err or "频次" in err:
                        wait = 30
                        logger.warning(f"Tushare 频率限制，等待 {wait}s 后重试（{attempt+1}/{max_retries}）...")
                        time.sleep(wait)
                    else:
                        raise
        # 最后一次不捕获异常，让调用者处理
        with _tushare_semaphore:
            result = fn(*args, **kwargs)
            self._api_sleep()
            return result

    # ========== 交易日历（缓存优先） ==========

    def _ensure_trade_cal(self):
        """确保交易日历已缓存，增量补数据"""
        today = datetime.now().strftime('%Y%m%d')
        year_start = datetime.now().strftime('%Y') + '0101'

        # 检查本月数据是否已有
        this_month = datetime.now().strftime('%Y%m')
        if db_cache.has_trade_cal(this_month):
            return

        # 获取整年交易日历
        logger.info("从 Tushare 获取交易日历...")
        try:
            cal = self._call_api(
                self.pro.trade_cal,
                exchange='SSE',
                start_date=(datetime.now() - timedelta(days=365)).strftime('%Y%m%d'),
                end_date=today
            )
            if cal is not None and not cal.empty:
                db_cache.save_trade_cal(cal)
        except Exception as e:
            logger.warning(f"获取交易日历异常: {e}")

    def _get_latest_trade_date(self) -> str:
        """获取最近有行情数据的交易日（非未来/未开盘日）"""
        self._ensure_trade_cal()
        today = datetime.now().strftime('%Y%m%d')
        now_hour = datetime.now().hour
        start = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
        dates = db_cache.get_trade_dates(start, today)
        if not dates:
            return today
        # 如果现在是交易时间前（15:30前）且今天是交易日，用前一交易日
        # 避免拉取今日尚未生成的数据
        if len(dates) >= 1:
            last_date = dates[-1]
            # 如果最后一个交易日是今天，且现在还没到15:30，则用前一个
            if last_date == today and now_hour < 16 and len(dates) >= 2:
                # 优先用缓存里有数据的最近日期
                for d in reversed(dates[:-1]):
                    if db_cache.has_daily(d):
                        return d
                return dates[-2]
            # 否则如果今天有缓存，直接用今天
            if last_date == today and db_cache.has_daily(today):
                return today
            # 今天没缓存，找最近有缓存的交易日
            for d in reversed(dates):
                if db_cache.has_daily(d):
                    return d
        return dates[-1]

    def _nearest_trade_date_before(self, date_str: str) -> str:
        """将任意日期归一到不晚于该日期的最近交易日"""
        self._ensure_trade_cal()
        normalized = (date_str or '').replace('-', '')
        if len(normalized) != 8 or not normalized.isdigit():
            return normalized
        target = datetime.strptime(normalized, '%Y%m%d')
        start = (target - timedelta(days=30)).strftime('%Y%m%d')
        dates = db_cache.get_trade_dates(start, normalized)
        if dates:
            return dates[-1]
        return normalized

    def _resolve_trade_date(self, trade_date: str = None) -> str:
        """解析显式日期或回测环境变量，否则回退到最新交易日"""
        requested = trade_date or os.environ.get('BBBIG_BACKTEST_DATE', '').strip()
        if requested:
            return self._nearest_trade_date_before(requested)
        return self._get_latest_trade_date()

    # ========== 股票基础信息（缓存优先） ==========

    def _ensure_stock_basic(self):
        """确保股票基础信息已缓存（24小时更新一次）"""
        if db_cache.is_stock_basic_fresh(max_age_hours=24):
            return

        logger.info("从 Tushare 获取股票基础信息...")
        try:
            df = self._call_api(
                self.pro.stock_basic,
                exchange='', list_status='L',
                fields='ts_code,symbol,name,area,industry,market,list_date'
            )
            if df is not None and not df.empty:
                db_cache.save_stock_basic(df)
        except Exception as e:
            logger.warning(f"获取股票基础信息异常: {e}")

    def _get_stock_basic(self) -> pd.DataFrame:
        """获取股票基础信息"""
        self._ensure_stock_basic()
        return db_cache.get_stock_basic()

    # ========== 日行情（增量获取） ==========

    def _ensure_daily(self, trade_date: str):
        """确保某日日行情已缓存"""
        if db_cache.has_daily(trade_date):
            return

        logger.info(f"从 Tushare 获取 {trade_date} 日行情...")
        try:
            df = self._call_api(self.pro.daily, trade_date=trade_date)
            if df is not None and not df.empty:
                db_cache.save_daily(df)
                db_cache.mark_synced('daily', trade_date)
                logger.info(f"缓存 {trade_date} 日行情 {len(df)} 条")
        except Exception as e:
            logger.error(f"获取 {trade_date} 日行情异常: {e}")

    def _ensure_daily_range(self, ts_code: str, start_date: str, end_date: str):
        """确保某只股票某段时间的日行情已缓存（增量补数据）"""
        self._ensure_trade_cal()
        missing_dates = db_cache.get_missing_trade_dates('daily', start_date, end_date)

        if not missing_dates:
            return

        # 检查该股票在这些日期是否已有数据
        cached = db_cache.get_daily_by_code(ts_code, start_date, end_date)
        cached_dates = set(cached['trade_date'].tolist()) if not cached.empty else set()

        # 只获取确实缺失的日期（按日期批量获取全市场数据）
        truly_missing = [d for d in missing_dates if d not in cached_dates]

        if not truly_missing:
            return

        # 如果缺失天数少，逐日获取全市场；如果多，按股票获取
        if len(truly_missing) <= 5:
            for date in truly_missing:
                self._ensure_daily(date)
        else:
            # 按股票代码获取区间数据
            logger.info(f"从 Tushare 获取 {ts_code} [{start_date}~{end_date}] K线...")
            try:
                df = self._call_api(self.pro.daily, ts_code=ts_code, start_date=start_date, end_date=end_date)
                if df is not None and not df.empty:
                    db_cache.save_daily(df)
                    logger.info(f"缓存 {ts_code} 日行情 {len(df)} 条")
            except Exception as e:
                logger.error(f"获取 {ts_code} 日行情异常: {e}")

    def _ensure_daily_basic(self, trade_date: str):
        """确保某日每日指标已缓存"""
        if db_cache.has_daily_basic(trade_date):
            return

        logger.info(f"从 Tushare 获取 {trade_date} 每日指标...")
        try:
            df = self._call_api(
                self.pro.daily_basic,
                trade_date=trade_date,
                fields='ts_code,turnover_rate,pe_ttm,pb,ps_ttm,total_mv,circ_mv,volume_ratio'
            )
            if df is not None and not df.empty:
                db_cache.save_daily_basic(df)
                db_cache.mark_synced('daily_basic', trade_date)
                logger.info(f"缓存 {trade_date} 每日指标 {len(df)} 条")
        except Exception as e:
            logger.error(f"获取 {trade_date} 每日指标异常: {e}")

    # ========== 公开接口（与调用方完全兼容） ==========

    @staticmethod
    def _ts_code_to_symbol(ts_code: str) -> str:
        """000001.SZ -> 000001"""
        return ts_code.split('.')[0] if '.' in ts_code else ts_code

    @staticmethod
    def _symbol_to_ts_code(symbol: str) -> str:
        """000001 -> 000001.SZ / 000300(沪深300指数) -> 000300.SH"""
        # 已含后缀则直接返回
        if '.' in symbol:
            return symbol
        # 上交所知名指数白名单（这些 6 位代码是指数，不是深交所股票）
        SH_INDEX_CODES = {'000300', '000016', '000905', '000852', '399001', '399006'}
        if symbol in SH_INDEX_CODES:
            return f"{symbol}.SH"
        # 普通股票：6开头->上交所，其余->深交所
        if symbol.startswith('6'):
            return f"{symbol}.SH"
        return f"{symbol}.SZ"

    @staticmethod
    def is_a_stock(code: str) -> bool:
        """判断是否为A股"""
        return code.startswith(('600', '601', '603', '605', '000', '001', '002', '003', '300', '301'))

    def fetch_all_stocks(self, trade_date: str = None) -> pd.DataFrame:
        """
        获取全部A股实时行情
        返回列: 最新价, 涨跌幅, 涨跌额, 成交量, 成交额, 振幅, 换手率,
                市盈率动, 量比, 代码, 名称, 最高, 最低, 今开, 昨收,
                总市值, 流通市值, 市净率, 60日涨跌幅, 年初至今涨跌幅,
                所处行业, 每股收益, 每股净资产
        """
        try:
            trade_date = self._resolve_trade_date(trade_date)
            if not trade_date:
                return pd.DataFrame()

            logger.info(f"获取 {trade_date} 全市场行情...")

            # 增量获取：日行情 + 每日指标 + 基础信息
            self._ensure_daily(trade_date)
            self._ensure_daily_basic(trade_date)
            self._ensure_stock_basic()

            # 从本地 SQLite 读取
            daily_df = db_cache.get_daily_by_date(trade_date)
            if daily_df.empty:
                logger.error("本地缓存无日行情数据")
                return pd.DataFrame()

            basic_df = db_cache.get_daily_basic_by_date(trade_date)
            stock_basic = db_cache.get_stock_basic()

            # 合并数据
            if not basic_df.empty:
                df = daily_df.merge(basic_df, on='ts_code', how='left', suffixes=('', '_basic'))
                if 'trade_date_basic' in df.columns:
                    df.drop(columns=['trade_date_basic'], inplace=True)
            else:
                df = daily_df.copy()
                logger.warning("每日指标(daily_basic)缺失，使用降级估算")

            # 确保 daily_basic 字段存在（降级默认值）
            for col, default in [('turnover_rate', 5.0), ('pe_ttm', 30.0), ('pb', 3.0),
                                 ('ps_ttm', 0), ('total_mv', 0), ('circ_mv', 0), ('volume_ratio', 1.0)]:
                if col not in df.columns:
                    df[col] = default

            # total_mv 降级估算：用 close * vol * 100（粗略估算流通市值，单位万元）
            if df['total_mv'].sum() == 0 and 'close' in df.columns and 'vol' in df.columns:
                df['total_mv'] = (df['close'] * df['vol'] * 100 / 10000).clip(lower=0)
                df['circ_mv'] = df['total_mv']
                logger.info("使用 close*vol 估算市值")

            if not stock_basic.empty:
                df = df.merge(
                    stock_basic[['ts_code', 'name', 'industry']],
                    on='ts_code', how='left'
                )
            else:
                df['name'] = ''
                df['industry'] = ''

            # 60日涨跌幅和年初至今涨跌幅
            df['60日涨跌幅'] = 0.0
            df['年初至今涨跌幅'] = 0.0

            # 估算每股收益和每股净资产
            df['每股收益'] = 0.0
            df['每股净资产'] = 0.0
            if 'pe_ttm' in df.columns and 'close' in df.columns:
                df['每股收益'] = df.apply(
                    lambda r: r['close'] / r['pe_ttm'] if r['pe_ttm'] and r['pe_ttm'] != 0 else 0,
                    axis=1
                )
            if 'pb' in df.columns and 'close' in df.columns:
                df['每股净资产'] = df.apply(
                    lambda r: r['close'] / r['pb'] if r['pb'] and r['pb'] != 0 else 0,
                    axis=1
                )

            df['代码'] = df['ts_code'].apply(self._ts_code_to_symbol)

            # 构建结果（列名与原接口完全一致）
            result = pd.DataFrame()
            result['最新价'] = pd.to_numeric(df['close'], errors='coerce')
            result['涨跌幅'] = pd.to_numeric(df['pct_chg'], errors='coerce')
            result['涨跌额'] = pd.to_numeric(df['change'], errors='coerce')
            result['成交量'] = pd.to_numeric(df['vol'], errors='coerce') * 100
            result['成交额'] = pd.to_numeric(df['amount'], errors='coerce') * 1000
            if 'high' in df.columns and 'low' in df.columns and 'pre_close' in df.columns:
                result['振幅'] = ((df['high'] - df['low']) / df['pre_close'] * 100).round(2)
            else:
                result['振幅'] = 0.0
            result['换手率'] = pd.to_numeric(df.get('turnover_rate', 0), errors='coerce')
            result['市盈率动'] = pd.to_numeric(df.get('pe_ttm', 0), errors='coerce')
            result['量比'] = pd.to_numeric(df.get('volume_ratio', 0), errors='coerce')
            result['代码'] = df['代码']
            result['名称'] = df.get('name', '').fillna('')
            result['最高'] = pd.to_numeric(df['high'], errors='coerce')
            result['最低'] = pd.to_numeric(df['low'], errors='coerce')
            result['今开'] = pd.to_numeric(df['open'], errors='coerce')
            result['昨收'] = pd.to_numeric(df['pre_close'], errors='coerce')
            result['总市值'] = pd.to_numeric(df.get('total_mv', 0), errors='coerce') * 10000
            result['流通市值'] = pd.to_numeric(df.get('circ_mv', 0), errors='coerce') * 10000
            result['市净率'] = pd.to_numeric(df.get('pb', 0), errors='coerce')
            result['60日涨跌幅'] = df['60日涨跌幅']
            result['年初至今涨跌幅'] = df['年初至今涨跌幅']
            result['所处行业'] = df.get('industry', '').fillna('')
            result['每股收益'] = df['每股收益'].round(4)
            result['每股净资产'] = df['每股净资产'].round(4)

            # 过滤
            result = result[result['代码'].apply(self.is_a_stock)]
            result = result[result['最新价'].notna() & (result['最新价'] > 0)]
            result = result[~result['名称'].str.contains('ST', na=False)]
            result = result.reset_index(drop=True)

            logger.info(f"共获取 {len(result)} 只A股行情（缓存）")
            return result

        except Exception as e:
            logger.error(f"获取全部A股行情异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return pd.DataFrame()

    # 指数代码集合（使用 index_daily 接口而非 daily/pro_bar）
    INDEX_CODES = {'000300', '000016', '000905', '000852', '399001', '399006'}

    def _is_index(self, code: str) -> bool:
        """判断是否为指数代码"""
        return code.replace('.SH', '').replace('.SZ', '') in self.INDEX_CODES

    def fetch_stock_kline(self, code: str, days: int = 30, adjust: str = "qfq",
                          end_date_str: str = None) -> pd.DataFrame:
        """
        获取个股/指数日K线数据（缓存优先，增量补数据）
        返回列: 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率
        """
        try:
            ts_code = self._symbol_to_ts_code(code)
            is_index = self._is_index(code)

            if end_date_str:
                end_date = end_date_str
            else:
                end_date = self._resolve_trade_date()
                if not end_date:
                    end_date = datetime.now().strftime('%Y%m%d')

            start_date = (datetime.strptime(end_date, '%Y%m%d') - timedelta(days=int(days * 1.8) + 30)).strftime('%Y%m%d')

            if is_index:
                # 指数K线：SQLite 缓存优先
                df = db_cache.get_index_daily(ts_code, start_date, end_date)
                if df.empty:
                    logger.info(f"从 Tushare 获取指数 {ts_code} [{start_date}~{end_date}] K线...")
                    df = self._call_api(
                        self.pro.index_daily,
                        ts_code=ts_code, start_date=start_date, end_date=end_date
                    )
                    if df is None or df.empty:
                        logger.warning(f"未获取到指数 {code} 的K线数据")
                        return pd.DataFrame()
                    df = df.sort_values('trade_date').reset_index(drop=True)
                    # index_daily 没有 pre_close，补充计算
                    df['pre_close'] = df['close'].shift(1)
                    db_cache.save_index_daily(df)
                else:
                    df = df.sort_values('trade_date').reset_index(drop=True)
                    # 补充 pre_close
                    if 'pre_close' not in df.columns or df['pre_close'].isna().all():
                        df['pre_close'] = df['close'].shift(1)
            else:
                # SQLite 已有足够窗口数据时直接使用，避免因 sync_log 缺口触发不必要的补数
                df = db_cache.get_daily_by_code(ts_code, start_date, end_date)
                min_cached_rows = max(days, 20)
                if len(df) < min_cached_rows:
                    self._ensure_daily_range(ts_code, start_date, end_date)
                    df = db_cache.get_daily_by_code(ts_code, start_date, end_date)

                if df.empty:
                    # 缓存没有，直接从 API 获取单只股票
                    logger.info(f"缓存无 {code} 数据，从 Tushare 直接获取...")
                    adj_map = {"qfq": "qfq", "hfq": "hfq", "": None}
                    adj = adj_map.get(adjust, "qfq")
                    df = self._call_api(
                        ts.pro_bar,
                        ts_code=ts_code, start_date=start_date, end_date=end_date,
                        adj=adj, factors=['tor']
                    )
                    if df is not None and not df.empty:
                        db_cache.save_daily(df)
                        df = df.sort_values('trade_date').reset_index(drop=True)
                    else:
                        logger.warning(f"未获取到 {code} 的K线数据")
                        return pd.DataFrame()

            # 计算振幅
            df['振幅'] = 0.0
            mask = df['pre_close'].notna() & (df['pre_close'] > 0)
            df.loc[mask, '振幅'] = ((df.loc[mask, 'high'] - df.loc[mask, 'low']) / df.loc[mask, 'pre_close'] * 100).round(2)

            # 构建结果
            result = pd.DataFrame()
            result['日期'] = df['trade_date'].apply(lambda x: f"{x[:4]}-{x[4:6]}-{x[6:8]}")
            result['开盘'] = pd.to_numeric(df['open'], errors='coerce')
            result['收盘'] = pd.to_numeric(df['close'], errors='coerce')
            result['最高'] = pd.to_numeric(df['high'], errors='coerce')
            result['最低'] = pd.to_numeric(df['low'], errors='coerce')
            result['成交量'] = pd.to_numeric(df['vol'], errors='coerce') * 100
            result['成交额'] = pd.to_numeric(df['amount'], errors='coerce') * 1000
            result['振幅'] = df['振幅']
            result['涨跌幅'] = pd.to_numeric(df['pct_chg'], errors='coerce')
            result['涨跌额'] = pd.to_numeric(df['change'], errors='coerce')
            # 换手率：缓存中没有 tor 字段，置 0
            result['换手率'] = 0.0

            result = result.tail(days).reset_index(drop=True)
            return result

        except Exception as e:
            logger.error(f"获取{code}K线异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return pd.DataFrame()

    def fetch_hot_sectors(self, trade_date: str = None) -> pd.DataFrame:
        """
        获取行业板块资金流向
        优先使用 moneyflow_ind_ths（同花顺行业资金流），每日只调用一次后缓存到内存，
        降级为按行业分组统计
        返回列: 板块名称, 涨跌幅, 主力净流入, 主力净流入占比
        """
        try:
            trade_date = self._resolve_trade_date(trade_date)
            if not trade_date:
                return pd.DataFrame()

            # 检查内存缓存（同一天内不重复调用API）
            cache_key = f"_moneyflow_ind_ths_{trade_date}"
            if hasattr(self, cache_key):
                return getattr(self, cache_key)

            # SQLite 缓存优先
            cached_df = db_cache.get_moneyflow_ind(trade_date)
            if not cached_df.empty:
                df = cached_df
                df['主力净流入'] = pd.to_numeric(df['net_amount'], errors='coerce').fillna(0) * 1e8
                df['涨跌幅'] = pd.to_numeric(df['pct_change'], errors='coerce').fillna(0)
                df['板块名称'] = df['industry'].fillna(df['ts_code'])
                total_abs = df['主力净流入'].abs().sum()
                df['主力净流入占比'] = (df['主力净流入'] / total_abs * 100).round(2) if total_abs > 0 else 0.0
                df = df.sort_values('主力净流入', ascending=False).head(20)
                result = df[['板块名称', '涨跌幅', '主力净流入', '主力净流入占比']].reset_index(drop=True)
                setattr(self, cache_key, result)
                return result

            # 方案1：同花顺行业资金流向接口（moneyflow_ind_ths）
            try:
                df = self._call_api(self.pro.moneyflow_ind_ths, trade_date=trade_date)
                if df is not None and not df.empty:
                    # 写入 SQLite 缓存
                    db_cache.save_moneyflow_ind(df, trade_date)
                    # net_amount 单位: 亿元; 转为与其他模块一致的"元"
                    df['主力净流入'] = pd.to_numeric(df['net_amount'], errors='coerce').fillna(0) * 1e8
                    df['涨跌幅'] = pd.to_numeric(df['pct_change'], errors='coerce').fillna(0)
                    df['板块名称'] = df['industry'].fillna(df['ts_code'])
                    total_abs = df['主力净流入'].abs().sum()
                    df['主力净流入占比'] = (df['主力净流入'] / total_abs * 100).round(2) if total_abs > 0 else 0.0
                    df = df.sort_values('主力净流入', ascending=False).head(20)
                    result = df[['板块名称', '涨跌幅', '主力净流入', '主力净流入占比']].reset_index(drop=True)
                    setattr(self, cache_key, result)  # 缓存到内存
                    return result
            except Exception as e:
                logger.warning(f"moneyflow_ind_ths 接口异常: {e}")

            # 降级方案：通过缓存的日行情 + 基础信息按行业分组（无主力净流入数据）
            logger.info("行业资金流接口不可用，降级为按行业分组统计涨跌幅")
            result = self._calc_sector_stats_by_industry(trade_date)
            setattr(self, cache_key, result)
            return result

        except Exception as e:
            logger.error(f"获取行业板块资金流异常: {e}")
            return pd.DataFrame()

    def fetch_concept_sectors(self, trade_date: str = None) -> pd.DataFrame:
        """
        获取概念板块资金流向（缓存优先）
        返回列: 板块名称, 涨跌幅, 主力净流入, 主力净流入占比
        """
        try:
            trade_date = self._resolve_trade_date(trade_date)
            if not trade_date:
                return pd.DataFrame()

            # 确保日行情已缓存
            self._ensure_daily(trade_date)
            daily_df = db_cache.get_daily_by_date(trade_date)
            if daily_df.empty:
                return pd.DataFrame()

            # 概念板块列表（缓存优先）
            concepts = pd.DataFrame()
            if db_cache.is_concept_fresh():
                concepts = db_cache.get_concepts()

            if concepts.empty:
                logger.info("从 Tushare 获取概念板块列表...")
                try:
                    concepts = self._call_api(self.pro.concept)
                    if concepts is not None and not concepts.empty:
                        db_cache.save_concepts(concepts)
                except Exception as e:
                    logger.error(f"获取概念列表异常: {e}")
                    return pd.DataFrame()

            if concepts.empty:
                return pd.DataFrame()

            # 个股资金流向（SQLite 缓存优先）
            moneyflow_df = None
            if db_cache.has_moneyflow(trade_date):
                moneyflow_df = db_cache.get_moneyflow(trade_date)
            else:
                try:
                    moneyflow_df = self._call_api(self.pro.moneyflow, trade_date=trade_date)
                    if moneyflow_df is not None and not moneyflow_df.empty:
                        db_cache.save_moneyflow(moneyflow_df, trade_date)
                except Exception:
                    pass

            results = []
            for _, concept in concepts.head(30).iterrows():
                try:
                    concept_code = concept['code']
                    # 成分股（缓存优先）
                    codes = db_cache.get_concept_detail(concept_code)
                    if not codes:
                        detail = self._call_api(
                            self.pro.concept_detail, id=concept_code, fields='ts_code')
                        if detail is not None and not detail.empty:
                            codes = detail['ts_code'].tolist()
                            db_cache.save_concept_detail(concept_code, codes)
                        else:
                            continue

                    sector_daily = daily_df[daily_df['ts_code'].isin(codes)]
                    if sector_daily.empty:
                        continue

                    avg_chg = sector_daily['pct_chg'].mean()
                    total_amount = sector_daily['amount'].sum() * 1000

                    net_inflow = 0.0
                    net_inflow_pct = 0.0
                    if moneyflow_df is not None and not moneyflow_df.empty:
                        sector_flow = moneyflow_df[moneyflow_df['ts_code'].isin(codes)]
                        if not sector_flow.empty:
                            if 'net_mf_amount' in sector_flow.columns:
                                net_inflow = sector_flow['net_mf_amount'].sum() * 10000
                            elif 'buy_elg_amount' in sector_flow.columns:
                                net_inflow = ((sector_flow['buy_elg_amount'] + sector_flow['buy_lg_amount']
                                               - sector_flow['sell_elg_amount'] - sector_flow['sell_lg_amount']).sum() * 10000)
                            if total_amount > 0:
                                net_inflow_pct = round(net_inflow / total_amount * 100, 2)

                    results.append({
                        '板块名称': concept['name'],
                        '涨跌幅': round(avg_chg, 2),
                        '主力净流入': net_inflow,
                        '主力净流入占比': net_inflow_pct,
                    })
                except Exception as e:
                    logger.debug(f"获取概念 {concept.get('name', '')} 详情异常: {e}")
                    continue

            if not results:
                return pd.DataFrame()

            df = pd.DataFrame(results)
            df = df.sort_values('主力净流入', ascending=False).head(20)
            return df[['板块名称', '涨跌幅', '主力净流入', '主力净流入占比']].reset_index(drop=True)

        except Exception as e:
            logger.error(f"获取概念板块资金流异常: {e}")
            return pd.DataFrame()

    # ========== 内部辅助方法 ==========

    def _get_industry_name_map(self) -> dict:
        stock_basic = self._get_stock_basic()
        if stock_basic.empty:
            return {}
        return {ind: ind for ind in stock_basic['industry'].dropna().unique()}

    # ========== 财务指标 ==========

    def fetch_fina_indicator(self, ts_code: str, force: bool = False) -> pd.DataFrame:
        """
        获取个股核心财务指标（缓存优先，7天有效）
        返回最近4个季度的: ROE, 扣非ROE, 毛利率, 净利润同比增速, 营收同比增速, 经营现金流/净利润, 流动比率
        """
        if not force and db_cache.is_fina_fresh(ts_code, max_age_hours=168):
            return db_cache.get_fina_indicator(ts_code, limit=4)

        try:
            logger.debug(f"从 Tushare 获取 {ts_code} 财务指标...")
            df = self._call_api(
                self.pro.fina_indicator,
                ts_code=ts_code,
                fields='ts_code,ann_date,end_date,roe,roe_dt,grossprofit_margin,'
                       'netprofit_yoy,revenue_yoy,ocf_to_profit,current_ratio',
                limit=4
            )
            if df is not None and not df.empty:
                db_cache.save_fina_indicator(df)
                return df
        except Exception as e:
            logger.debug(f"获取 {ts_code} 财务指标异常: {e}")

        # 回退到缓存（即使过期也用）
        return db_cache.get_fina_indicator(ts_code, limit=4)

    def fetch_fina_batch(self, ts_codes: list) -> dict:
        """
        批量获取多只股票的财务指标摘要
        返回 {ts_code: {roe, revenue_yoy, netprofit_yoy, grossprofit_margin, ocf_to_profit, current_ratio}}

        策略：先从缓存批量取，缺失的再逐只补充（限速）
        """
        fina_map = {}

        # 1. 先尝试批量从缓存获取
        cached = db_cache.get_fina_indicator_batch(ts_codes)
        if not cached.empty:
            for _, row in cached.iterrows():
                code = row['ts_code']
                fina_map[code] = {
                    'roe': row.get('roe', None),
                    'roe_dt': row.get('roe_dt', None),
                    'grossprofit_margin': row.get('grossprofit_margin', None),
                    'netprofit_yoy': row.get('netprofit_yoy', None),
                    'revenue_yoy': row.get('revenue_yoy', None),
                    'ocf_to_profit': row.get('ocf_to_profit', None),
                    'current_ratio': row.get('current_ratio', None),
                }

        # 2. 缺失的逐只从API获取（限速，最多补50只）
        missing = [c for c in ts_codes if c not in fina_map]
        if missing:
            logger.info(f"  财务指标缓存命中 {len(fina_map)} 只，需补充 {len(missing)} 只")
            for i, code in enumerate(missing[:50]):
                try:
                    time.sleep(random.uniform(0.2, 0.5))
                    df = self.fetch_fina_indicator(code)
                    if not df.empty:
                        latest = df.iloc[0]
                        fina_map[code] = {
                            'roe': latest.get('roe', None),
                            'roe_dt': latest.get('roe_dt', None),
                            'grossprofit_margin': latest.get('grossprofit_margin', None),
                            'netprofit_yoy': latest.get('netprofit_yoy', None),
                            'revenue_yoy': latest.get('revenue_yoy', None),
                            'ocf_to_profit': latest.get('ocf_to_profit', None),
                            'current_ratio': latest.get('current_ratio', None),
                        }
                except Exception as e:
                    logger.debug(f"补充获取 {code} 财务数据失败: {e}")
                if (i + 1) % 20 == 0:
                    logger.info(f"  财务指标补充进度: {i+1}/{len(missing[:50])}")

        return fina_map

    def _calc_sector_stats_by_industry(self, trade_date: str) -> pd.DataFrame:
        """通过行业分组计算板块统计（降级方案，使用缓存）"""
        try:
            self._ensure_daily(trade_date)
            self._ensure_stock_basic()

            daily_df = db_cache.get_daily_by_date(trade_date)
            stock_basic = db_cache.get_stock_basic()

            if daily_df.empty:
                return pd.DataFrame()

            if stock_basic.empty or 'industry' not in stock_basic.columns:
                return pd.DataFrame()

            merged = daily_df.merge(stock_basic[['ts_code', 'industry']], on='ts_code', how='left')
            merged = merged.dropna(subset=['industry'])

            stats = merged.groupby('industry').agg(
                涨跌幅=('pct_chg', 'mean'),
                成交额=('amount', 'sum')
            ).reset_index()

            stats['板块名称'] = stats['industry']
            stats['主力净流入'] = 0.0
            stats['主力净流入占比'] = 0.0
            stats['涨跌幅'] = stats['涨跌幅'].round(2)

            stats = stats.sort_values('涨跌幅', ascending=False).head(20)
            return stats[['板块名称', '涨跌幅', '主力净流入', '主力净流入占比']].reset_index(drop=True)

        except Exception as e:
            logger.error(f"计算行业统计异常: {e}")
            return pd.DataFrame()


# 全局实例
fetcher = StockDataFetcher()


if __name__ == "__main__":
    print("=== 获取A股实时行情 ===")
    stocks = fetcher.fetch_all_stocks()
    print(f"共获取 {len(stocks)} 只A股")
    print(stocks.head())

    print("\n=== 获取平安银行K线 ===")
    kline = fetcher.fetch_stock_kline("000001", days=10)
    print(kline)

    print("\n=== 行业板块资金流向 ===")
    sectors = fetcher.fetch_hot_sectors()
    print(sectors)

    print("\n=== 数据库统计 ===")
    print(db_cache.get_db_stats())
