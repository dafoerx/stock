#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据获取模块 - 基于 Tushare Pro
获取A股实时行情、历史K线、板块资金流向数据
"""
import time
import logging
import tushare as ts
import pandas as pd
from datetime import datetime, timedelta

from BBBIG.config import TUSHARE_TOKEN

logger = logging.getLogger("BBBIG")


class StockDataFetcher:
    """A股数据获取器 (Tushare Pro)"""

    def __init__(self):
        ts.set_token(TUSHARE_TOKEN)
        self.pro = ts.pro_api()
        self._stock_basic_cache = None
        self._stock_basic_cache_time = None
        self._cache_ttl = 3600  # 缓存1小时

    def _get_stock_basic(self) -> pd.DataFrame:
        """获取股票基础信息（带缓存）"""
        now = time.time()
        if (self._stock_basic_cache is not None
                and self._stock_basic_cache_time
                and now - self._stock_basic_cache_time < self._cache_ttl):
            return self._stock_basic_cache

        try:
            df = self.pro.stock_basic(
                exchange='', list_status='L',
                fields='ts_code,symbol,name,area,industry,market,list_date'
            )
            self._stock_basic_cache = df
            self._stock_basic_cache_time = now
            return df
        except Exception as e:
            logger.error(f"获取股票基础信息异常: {e}")
            return pd.DataFrame()

    @staticmethod
    def _ts_code_to_symbol(ts_code: str) -> str:
        """000001.SZ -> 000001"""
        return ts_code.split('.')[0] if '.' in ts_code else ts_code

    @staticmethod
    def _symbol_to_ts_code(symbol: str) -> str:
        """000001 -> 000001.SZ"""
        if symbol.startswith(('6',)):
            return f"{symbol}.SH"
        else:
            return f"{symbol}.SZ"

    @staticmethod
    def is_a_stock(code: str) -> bool:
        """判断是否为A股"""
        return code.startswith(('600', '601', '603', '605', '000', '001', '002', '003', '300', '301'))

    def fetch_all_stocks(self) -> pd.DataFrame:
        """
        获取全部A股实时行情
        返回列: 最新价, 涨跌幅, 涨跌额, 成交量, 成交额, 振幅, 换手率,
                市盈率动, 量比, 代码, 名称, 最高, 最低, 今开, 昨收,
                总市值, 流通市值, 市净率, 60日涨跌幅, 年初至今涨跌幅,
                所处行业, 每股收益, 每股净资产
        """
        try:
            # 获取最近交易日
            trade_date = self._get_latest_trade_date()
            if not trade_date:
                return pd.DataFrame()

            logger.info(f"获取 {trade_date} 全市场行情...")

            # 日行情
            daily_df = self.pro.daily(trade_date=trade_date)
            if daily_df is None or daily_df.empty:
                return pd.DataFrame()
            time.sleep(0.3)

            # 每日指标（换手率、市盈率、市净率、市值等）
            basic_df = self.pro.daily_basic(
                trade_date=trade_date,
                fields='ts_code,turnover_rate,pe_ttm,pb,ps_ttm,total_mv,circ_mv,volume_ratio'
            )
            time.sleep(0.3)

            # 股票基础信息（行业、名称）
            stock_basic = self._get_stock_basic()

            # 合并数据
            df = daily_df.merge(basic_df, on='ts_code', how='left', suffixes=('', '_basic'))
            df = df.merge(
                stock_basic[['ts_code', 'name', 'industry']],
                on='ts_code', how='left'
            )

            # 计算60日涨跌幅
            df['60日涨跌幅'] = 0.0
            df['年初至今涨跌幅'] = 0.0

            # 尝试获取60日涨跌幅（通过stk_factor或计算）
            try:
                # 获取60日前的日期
                date_60 = (datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=90)).strftime('%Y%m%d')
                # 批量获取比较复杂，先设为0，后续有需要再优化
            except Exception:
                pass

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

            # 转换代码格式
            df['代码'] = df['ts_code'].apply(self._ts_code_to_symbol)

            # 重命名列以匹配原接口
            result = pd.DataFrame()
            result['最新价'] = pd.to_numeric(df['close'], errors='coerce')
            result['涨跌幅'] = pd.to_numeric(df['pct_chg'], errors='coerce')
            result['涨跌额'] = pd.to_numeric(df['change'], errors='coerce')
            result['成交量'] = pd.to_numeric(df['vol'], errors='coerce') * 100  # tushare单位是手，转为股
            result['成交额'] = pd.to_numeric(df['amount'], errors='coerce') * 1000  # tushare单位是千元，转为元
            result['振幅'] = pd.to_numeric(df.get('pct_chg', 0), errors='coerce')  # 近似
            # 计算振幅 = (最高-最低)/昨收*100
            if 'high' in df.columns and 'low' in df.columns and 'pre_close' in df.columns:
                result['振幅'] = ((df['high'] - df['low']) / df['pre_close'] * 100).round(2)
            result['换手率'] = pd.to_numeric(df.get('turnover_rate', 0), errors='coerce')
            result['市盈率动'] = pd.to_numeric(df.get('pe_ttm', 0), errors='coerce')
            result['量比'] = pd.to_numeric(df.get('volume_ratio', 0), errors='coerce')
            result['代码'] = df['代码']
            result['名称'] = df['name']
            result['最高'] = pd.to_numeric(df['high'], errors='coerce')
            result['最低'] = pd.to_numeric(df['low'], errors='coerce')
            result['今开'] = pd.to_numeric(df['open'], errors='coerce')
            result['昨收'] = pd.to_numeric(df['pre_close'], errors='coerce')
            result['总市值'] = pd.to_numeric(df.get('total_mv', 0), errors='coerce') * 10000  # 万元转元
            result['流通市值'] = pd.to_numeric(df.get('circ_mv', 0), errors='coerce') * 10000
            result['市净率'] = pd.to_numeric(df.get('pb', 0), errors='coerce')
            result['60日涨跌幅'] = df['60日涨跌幅']
            result['年初至今涨跌幅'] = df['年初至今涨跌幅']
            result['所处行业'] = df['industry'].fillna('')
            result['每股收益'] = df['每股收益'].round(4)
            result['每股净资产'] = df['每股净资产'].round(4)

            # 过滤A股 + 有价格
            result = result[result['代码'].apply(self.is_a_stock)]
            result = result[result['最新价'].notna() & (result['最新价'] > 0)]
            # 过滤ST
            result = result[~result['名称'].str.contains('ST', na=False)]
            result = result.reset_index(drop=True)

            logger.info(f"共获取 {len(result)} 只A股行情")
            return result

        except Exception as e:
            logger.error(f"获取全部A股行情异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return pd.DataFrame()

    def fetch_stock_kline(self, code: str, days: int = 30, adjust: str = "qfq",
                          end_date_str: str = None) -> pd.DataFrame:
        """
        获取个股日K线数据
        :param code: 股票代码（如 000001）
        :param days: 获取天数
        :param adjust: qfq-前复权, hfq-后复权, 空-不复权
        :param end_date_str: 结束日期，格式 YYYYMMDD，默认为最近交易日
        返回列: 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率
        """
        try:
            ts_code = self._symbol_to_ts_code(code)

            if end_date_str:
                end_date = end_date_str
            else:
                end_date = self._get_latest_trade_date()
                if not end_date:
                    end_date = datetime.now().strftime('%Y%m%d')

            # 多取一些交易日数据以确保够用
            start_date = (datetime.strptime(end_date, '%Y%m%d') - timedelta(days=int(days * 1.8) + 30)).strftime('%Y%m%d')

            # 使用 ts.pro_bar 获取复权数据
            adj_map = {"qfq": "qfq", "hfq": "hfq", "": None}
            adj = adj_map.get(adjust, "qfq")

            df = ts.pro_bar(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
                adj=adj,
                factors=['tor']  # 换手率
            )

            if df is None or df.empty:
                logger.warning(f"未获取到 {code} 的K线数据")
                return pd.DataFrame()

            # 按日期升序排列
            df = df.sort_values('trade_date').reset_index(drop=True)

            # 计算振幅
            df['振幅'] = ((df['high'] - df['low']) / df['pre_close'] * 100).round(2)

            # 构建结果 DataFrame
            result = pd.DataFrame()
            result['日期'] = df['trade_date'].apply(lambda x: f"{x[:4]}-{x[4:6]}-{x[6:8]}")
            result['开盘'] = pd.to_numeric(df['open'], errors='coerce')
            result['收盘'] = pd.to_numeric(df['close'], errors='coerce')
            result['最高'] = pd.to_numeric(df['high'], errors='coerce')
            result['最低'] = pd.to_numeric(df['low'], errors='coerce')
            result['成交量'] = pd.to_numeric(df['vol'], errors='coerce') * 100  # 手转股
            result['成交额'] = pd.to_numeric(df['amount'], errors='coerce') * 1000  # 千元转元
            result['振幅'] = df['振幅']
            result['涨跌幅'] = pd.to_numeric(df['pct_chg'], errors='coerce')
            result['涨跌额'] = pd.to_numeric(df['change'], errors='coerce')
            result['换手率'] = pd.to_numeric(df.get('tor', pd.Series([0]*len(df))), errors='coerce').fillna(0)

            # 只取最近 days 个交易日
            result = result.tail(days).reset_index(drop=True)
            return result

        except Exception as e:
            logger.error(f"获取{code}K线异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return pd.DataFrame()

    def fetch_hot_sectors(self) -> pd.DataFrame:
        """
        获取行业板块资金流向
        返回列: 板块名称, 涨跌幅, 主力净流入, 主力净流入占比
        """
        try:
            trade_date = self._get_latest_trade_date()
            if not trade_date:
                return pd.DataFrame()

            # 获取行业资金流向
            df = self.pro.moneyflow_ind(trade_date=trade_date)
            time.sleep(0.3)

            if df is None or df.empty:
                # 降级方案：通过行业分组计算
                return self._calc_sector_stats_by_industry(trade_date)

            # 主力净流入 = 超大单 + 大单
            df['主力净流入'] = (df['super_net_inflow'] + df['big_net_inflow']) * 10000  # 万元转元
            total_flow = df['buy_elg_amount'] + df['buy_lg_amount'] + df['sell_elg_amount'] + df['sell_lg_amount']
            df['主力净流入占比'] = (df['主力净流入'] / (total_flow * 10000).replace(0, float('nan')) * 100).round(2)

            # 用行业代码获取行业名称
            industry_map = self._get_industry_name_map()
            df['板块名称'] = df['industry'].map(industry_map).fillna(df['industry'])

            # 获取行业涨跌幅（通过当日行业个股平均涨跌幅）
            df['涨跌幅'] = 0.0
            try:
                daily_df = self.pro.daily(trade_date=trade_date, fields='ts_code,pct_chg')
                stock_basic = self._get_stock_basic()
                if daily_df is not None and not daily_df.empty:
                    merged = daily_df.merge(stock_basic[['ts_code', 'industry']], on='ts_code', how='left')
                    industry_chg = merged.groupby('industry')['pct_chg'].mean().reset_index()
                    industry_chg.columns = ['industry', '涨跌幅']
                    df = df.merge(industry_chg, on='industry', how='left', suffixes=('_old', ''))
                    if '涨跌幅_old' in df.columns:
                        df.drop(columns=['涨跌幅_old'], inplace=True)
            except Exception:
                pass

            df = df.sort_values('主力净流入', ascending=False).head(20)
            return df[['板块名称', '涨跌幅', '主力净流入', '主力净流入占比']].reset_index(drop=True)

        except Exception as e:
            logger.error(f"获取行业板块资金流异常: {e}")
            # 降级方案
            try:
                trade_date = self._get_latest_trade_date()
                if trade_date:
                    return self._calc_sector_stats_by_industry(trade_date)
            except Exception:
                pass
            return pd.DataFrame()

    def fetch_concept_sectors(self) -> pd.DataFrame:
        """
        获取概念板块资金流向
        返回列: 板块名称, 涨跌幅, 主力净流入, 主力净流入占比
        """
        try:
            trade_date = self._get_latest_trade_date()
            if not trade_date:
                return pd.DataFrame()

            # 获取概念板块列表
            concepts = self.pro.concept()
            time.sleep(0.3)

            if concepts is None or concepts.empty:
                return pd.DataFrame()

            # 获取当日行情
            daily_df = self.pro.daily(trade_date=trade_date, fields='ts_code,pct_chg,amount')
            time.sleep(0.3)

            if daily_df is None or daily_df.empty:
                return pd.DataFrame()

            # 获取资金流向数据
            moneyflow_df = None
            try:
                moneyflow_df = self.pro.moneyflow(trade_date=trade_date)
                time.sleep(0.3)
            except Exception:
                pass

            results = []
            # 只分析前30个概念板块（避免接口限流）
            for _, concept in concepts.head(30).iterrows():
                try:
                    time.sleep(0.5)  # 避免限流
                    detail = self.pro.concept_detail(id=concept['code'], fields='ts_code')
                    if detail is None or detail.empty:
                        continue

                    codes = detail['ts_code'].tolist()
                    sector_daily = daily_df[daily_df['ts_code'].isin(codes)]

                    if sector_daily.empty:
                        continue

                    avg_chg = sector_daily['pct_chg'].mean()
                    total_amount = sector_daily['amount'].sum() * 1000  # 千元转元

                    # 如果有资金流向数据
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

    def _get_latest_trade_date(self) -> str:
        """获取最近交易日"""
        try:
            today = datetime.now().strftime('%Y%m%d')
            cal = self.pro.trade_cal(
                exchange='SSE',
                start_date=(datetime.now() - timedelta(days=30)).strftime('%Y%m%d'),
                end_date=today,
                is_open='1'
            )
            if cal is not None and not cal.empty:
                return cal['cal_date'].max()
        except Exception as e:
            logger.warning(f"获取交易日历异常: {e}")

        # 降级：返回今天
        return datetime.now().strftime('%Y%m%d')

    def _get_industry_name_map(self) -> dict:
        """获取行业代码到名称的映射"""
        stock_basic = self._get_stock_basic()
        if stock_basic.empty:
            return {}
        # tushare的industry字段本身就是中文名称
        return {ind: ind for ind in stock_basic['industry'].dropna().unique()}

    def _calc_sector_stats_by_industry(self, trade_date: str) -> pd.DataFrame:
        """通过行业分组计算板块统计（降级方案）"""
        try:
            daily_df = self.pro.daily(trade_date=trade_date, fields='ts_code,pct_chg,amount')
            stock_basic = self._get_stock_basic()

            if daily_df is None or daily_df.empty:
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
