#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BBBIG 数据预缓存脚本
========================================
将选股/回测所需的所有数据批量缓存到 SQLite，
避免正式运行时触发 Tushare IP 超限。

使用方式：
  python -m BBBIG.precache              # 缓存近30天 + 近60只个股K线
  python -m BBBIG.precache --days 60    # 自定义市场行情天数
  python -m BBBIG.precache --kline 120  # 个股K线天数（回测需要更长）
  python -m BBBIG.precache --full       # 完整模式：近90天行情+近120天个股K线

并发策略：
  - 全市场日行情/指标：串行，每次调用间隔 1.2s（Tushare 限速）
  - 个股 K线：有限并发（3线程），每线程间隔 1.0s
"""

import sys
import os
import time
import logging
import argparse
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tushare as ts
import pandas as pd

from BBBIG.config import TUSHARE_TOKEN, MIN_MARKET_CAP, MIN_VOLUME
from BBBIG.db_cache import db_cache

# ─── 日志 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("precache")

# ─── 限流器 ────────────────────────────────────────────────────────────────────
_api_lock = threading.Lock()
_last_call_time = 0.0
API_MIN_INTERVAL = 1.0   # 全局最小调用间隔（秒），对应单连接限速
API_ERR_BACKOFF  = 5.0   # IP超限时退避等待（秒）
API_MAX_RETRY    = 6     # 单次请求最大重试次数


def rate_limited_call(fn, *args, **kwargs):
    """带限流和重试的 API 调用包装器"""
    global _last_call_time
    for attempt in range(API_MAX_RETRY):
        with _api_lock:
            elapsed = time.time() - _last_call_time
            if elapsed < API_MIN_INTERVAL:
                time.sleep(API_MIN_INTERVAL - elapsed)
            _last_call_time = time.time()
        try:
            result = fn(*args, **kwargs)
            return result
        except Exception as e:
            err = str(e)
            if "IP数量超限" in err or "ERROR" in err:
                wait = API_ERR_BACKOFF * (attempt + 1)
                logger.warning(f"  ⚠ IP超限，等待 {wait:.0f}s 后重试（第{attempt+1}/{API_MAX_RETRY}次）...")
                time.sleep(wait)
            else:
                logger.warning(f"  ⚠ API异常: {e}")
                if attempt < API_MAX_RETRY - 1:
                    time.sleep(2)
                else:
                    raise
    return None


# ─── 初始化 Tushare ─────────────────────────────────────────────────────────────
def init_tushare():
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()
    # 快速验证 token
    test = rate_limited_call(pro.trade_cal, exchange='SSE',
                             start_date='20260101', end_date='20260110')
    if test is None or test.empty:
        raise RuntimeError("Tushare token 验证失败，请检查 TUSHARE_TOKEN")
    logger.info("✅ Tushare 连接正常")
    return pro


# ─── Step 1: 交易日历 ───────────────────────────────────────────────────────────
def cache_trade_cal(pro, days: int):
    logger.info("─" * 50)
    logger.info("📅 [1/6] 缓存交易日历...")
    start = (datetime.now() - timedelta(days=days + 10)).strftime('%Y%m%d')
    end   = datetime.now().strftime('%Y%m%d')
    df = rate_limited_call(pro.trade_cal, exchange='SSE', start_date=start, end_date=end)
    if df is not None and not df.empty:
        db_cache.save_trade_cal(df)
        open_days = df[df['is_open'] == 1]
        logger.info(f"  ✅ 交易日历缓存完成，含 {len(open_days)} 个交易日")
        return df[df['is_open'] == 1]['cal_date'].tolist()
    else:
        logger.error("  ❌ 交易日历获取失败")
        return []


# ─── Step 2: 股票基础信息 ────────────────────────────────────────────────────────
def cache_stock_basic(pro):
    logger.info("─" * 50)
    logger.info("📋 [2/6] 缓存股票基础信息...")
    existing = db_cache.get_stock_basic()
    if not existing.empty:
        logger.info(f"  ✅ 已有 {len(existing)} 条，跳过（24h内不重复拉取）")
        return existing
    df = rate_limited_call(
        pro.stock_basic, exchange='', list_status='L',
        fields='ts_code,symbol,name,area,industry,market,list_date'
    )
    if df is not None and not df.empty:
        db_cache.save_stock_basic(df)
        logger.info(f"  ✅ 股票基础信息缓存完成，共 {len(df)} 只")
        return df
    else:
        logger.error("  ❌ 股票基础信息获取失败")
        return pd.DataFrame()


# ─── Step 3: 全市场日行情（批量按日期） ─────────────────────────────────────────
def cache_market_daily(pro, trade_dates: list):
    logger.info("─" * 50)
    logger.info(f"📊 [3/6] 缓存全市场日行情（共 {len(trade_dates)} 个交易日）...")
    # 过滤已缓存的日期
    missing = [d for d in trade_dates if not db_cache.has_daily(d)]
    if not missing:
        logger.info("  ✅ 全部已缓存，跳过")
        return
    logger.info(f"  需要拉取 {len(missing)} 个交易日的行情...")
    ok = 0
    for i, date in enumerate(missing):
        df = rate_limited_call(pro.daily, trade_date=date)
        if df is not None and not df.empty:
            db_cache.save_daily(df)
            db_cache.mark_synced('daily', date)
            ok += 1
            logger.info(f"  [{i+1}/{len(missing)}] {date} daily: {len(df)} 条 ✅")
        else:
            logger.warning(f"  [{i+1}/{len(missing)}] {date} daily: 无数据（可能非交易日）")
    logger.info(f"  ✅ 日行情缓存完成，成功 {ok}/{len(missing)} 天")


# ─── Step 4: 全市场每日指标（daily_basic） ──────────────────────────────────────
def cache_market_daily_basic(pro, trade_dates: list):
    logger.info("─" * 50)
    logger.info(f"📈 [4/6] 缓存全市场每日指标（共 {len(trade_dates)} 个交易日）...")
    missing = [d for d in trade_dates if not db_cache.has_daily_basic(d)]
    if not missing:
        logger.info("  ✅ 全部已缓存，跳过")
        return
    logger.info(f"  需要拉取 {len(missing)} 个交易日的指标...")
    ok = 0
    for i, date in enumerate(missing):
        df = rate_limited_call(
            pro.daily_basic, trade_date=date,
            fields='ts_code,trade_date,turnover_rate,pe_ttm,pb,ps_ttm,total_mv,circ_mv,volume_ratio'
        )
        if df is not None and not df.empty:
            db_cache.save_daily_basic(df)
            db_cache.mark_synced('daily_basic', date)
            ok += 1
            logger.info(f"  [{i+1}/{len(missing)}] {date} daily_basic: {len(df)} 条 ✅")
        else:
            logger.warning(f"  [{i+1}/{len(missing)}] {date} daily_basic: 无数据")
    logger.info(f"  ✅ 每日指标缓存完成，成功 {ok}/{len(missing)} 天")


# ─── Step 5: 大盘指数K线（用于风控） ────────────────────────────────────────────
def cache_index_kline(pro, start_date: str, end_date: str):
    logger.info("─" * 50)
    logger.info("📉 [5/6] 缓存大盘指数K线（沪深300/上证/创业板）...")
    indices = [
        ('000300.SH', '沪深300'),
        ('000001.SH', '上证指数'),
        ('399006.SZ', '创业板指'),
    ]
    for ts_code, name in indices:
        df = rate_limited_call(
            pro.index_daily, ts_code=ts_code,
            start_date=start_date, end_date=end_date
        )
        if df is not None and not df.empty:
            # 存入 daily 表（ts_code 本身区分）
            db_cache.save_daily(df)
            logger.info(f"  ✅ {name}({ts_code}): {len(df)} 条")
        else:
            logger.warning(f"  ⚠ {name}({ts_code}): 无数据")


# ─── Step 6: 热门个股K线（有限并发） ─────────────────────────────────────────────
def _get_hot_stock_codes(pro, trade_dates: list, top_n: int = 100) -> list:
    """从缓存的日行情中，按成交额排出近期最活跃的 top_n 只股票"""
    if not trade_dates:
        return []
    # 取最近5个交易日的数据做统计
    recent_dates = trade_dates[-5:]
    all_dfs = []
    for d in recent_dates:
        df = db_cache.get_daily_by_date(d)
        if not df.empty:
            all_dfs.append(df[['ts_code', 'amount']])
    if not all_dfs:
        return []
    merged = pd.concat(all_dfs)
    top = merged.groupby('ts_code')['amount'].sum().nlargest(top_n)
    codes = top.index.tolist()
    # 只保留 A股（去掉指数代码）
    codes = [c for c in codes if c[0].isdigit() and not c.startswith('000300')]
    logger.info(f"  近5日成交额 Top {len(codes)} 只活跃股")
    return codes


def _cache_single_kline(pro, ts_code: str, start_date: str, end_date: str, thread_id: int) -> bool:
    """缓存单只股票K线（供线程池调用）"""
    # 先检查是否已缓存
    existing = db_cache.get_daily_by_code(ts_code, start_date, end_date)
    if not existing.empty and len(existing) >= 10:
        return True  # 已有数据，跳过

    df = rate_limited_call(
        pro.daily, ts_code=ts_code,
        start_date=start_date, end_date=end_date
    )
    if df is not None and not df.empty:
        db_cache.save_daily(df)
        return True
    # 降级：用 pro_bar 获取（带复权）
    try:
        df2 = rate_limited_call(
            ts.pro_bar, ts_code=ts_code,
            start_date=start_date, end_date=end_date,
            adj='qfq', factors=['tor']
        )
        if df2 is not None and not df2.empty:
            db_cache.save_daily(df2)
            return True
    except Exception:
        pass
    return False


def cache_stock_klines(pro, trade_dates: list, kline_days: int, top_n: int = 100,
                       max_workers: int = 3):
    logger.info("─" * 50)
    logger.info(f"📦 [6/6] 缓存个股K线（Top {top_n} 活跃股，近 {kline_days} 天，{max_workers} 线程）...")

    codes = _get_hot_stock_codes(pro, trade_dates, top_n=top_n)
    if not codes:
        logger.warning("  ⚠ 无法获取活跃股列表，跳过个股K线缓存")
        return

    end_date   = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=kline_days + 5)).strftime('%Y%m%d')

    # 检查哪些股票已有足够数据
    need_fetch = []
    for code in codes:
        existing = db_cache.get_daily_by_code(code, start_date, end_date)
        if existing.empty or len(existing) < max(kline_days // 7 * 5, 10):
            need_fetch.append(code)

    if not need_fetch:
        logger.info(f"  ✅ {len(codes)} 只股票 K线均已缓存，跳过")
        return

    logger.info(f"  需要拉取 {len(need_fetch)}/{len(codes)} 只股票 K线...")

    ok = 0
    fail = 0
    done = 0
    total = len(need_fetch)

    # 使用有限线程池 + 计数锁
    results_lock = threading.Lock()

    def fetch_one(args):
        nonlocal ok, fail, done
        idx, ts_code = args
        success = _cache_single_kline(pro, ts_code, start_date, end_date, idx % max_workers)
        with results_lock:
            done += 1
            if success:
                ok += 1
            else:
                fail += 1
            if done % 10 == 0 or done == total:
                logger.info(f"  进度: {done}/{total} | ✅{ok} ❌{fail}")
        return ts_code, success

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_one, (i, code))
                   for i, code in enumerate(need_fetch)]
        for _ in as_completed(futures):
            pass  # 通过 fetch_one 内部打印进度

    logger.info(f"  ✅ 个股K线缓存完成，成功 {ok}/{total}，失败 {fail}")


# ─── 打印缓存统计 ─────────────────────────────────────────────────────────────────
def print_stats():
    stats = db_cache.get_db_stats()
    logger.info("─" * 50)
    logger.info("📊 SQLite 缓存统计：")
    table_names = {
        'trade_cal': '交易日历',
        'stock_basic': '股票基础信息',
        'daily': '日行情',
        'daily_basic': '每日指标',
        'concept': '概念板块',
        'concept_detail': '概念成分股',
        'sync_log': '同步记录',
    }
    for table, cnt in stats.items():
        logger.info(f"  {table_names.get(table, table):12s}: {cnt:>8,} 条")
    # 数据库文件大小
    from BBBIG.config import DB_FILE
    if os.path.exists(DB_FILE):
        size_mb = os.path.getsize(DB_FILE) / 1024 / 1024
        logger.info(f"  {'数据库文件':12s}: {size_mb:.1f} MB  ({DB_FILE})")


# ─── 主入口 ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="BBBIG 数据预缓存脚本")
    parser.add_argument('--days',    type=int, default=30,  help='全市场行情缓存天数（默认30）')
    parser.add_argument('--kline',   type=int, default=60,  help='个股K线天数（默认60，回测建议120）')
    parser.add_argument('--topn',    type=int, default=100, help='活跃股数量（默认100）')
    parser.add_argument('--workers', type=int, default=3,   help='个股K线并发线程数（默认3，最多5）')
    parser.add_argument('--full',    action='store_true',   help='完整模式：90天行情+120天个股K线+200只股')
    parser.add_argument('--stats',   action='store_true',   help='仅打印当前缓存统计')
    args = parser.parse_args()

    if args.stats:
        print_stats()
        return

    if args.full:
        args.days    = 90
        args.kline   = 120
        args.topn    = 200
        args.workers = 3

    # 限制并发数，避免超限
    args.workers = min(args.workers, 5)

    logger.info("=" * 50)
    logger.info("🚀 BBBIG 数据预缓存开始")
    logger.info(f"   行情天数: {args.days} 天 | K线天数: {args.kline} 天")
    logger.info(f"   活跃股数: {args.topn} 只 | 并发线程: {args.workers}")
    logger.info("=" * 50)

    t0 = time.time()

    # 初始化 Tushare
    try:
        pro = init_tushare()
    except Exception as e:
        logger.error(f"❌ 初始化失败: {e}")
        sys.exit(1)

    # 计算日期范围
    end_date   = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=args.days)).strftime('%Y%m%d')

    # Step 1: 交易日历
    trade_dates = cache_trade_cal(pro, args.days)
    if not trade_dates:
        logger.error("❌ 无法获取交易日历，退出")
        sys.exit(1)
    # 只保留目标范围内的交易日
    trade_dates = [d for d in trade_dates if start_date <= d <= end_date]
    trade_dates = sorted(trade_dates)  # 确保升序
    logger.info(f"  目标范围内交易日: {len(trade_dates)} 天 ({trade_dates[0]} ~ {trade_dates[-1]})")

    # Step 2: 股票基础信息
    cache_stock_basic(pro)
    time.sleep(2)

    # Step 3: 全市场日行情
    cache_market_daily(pro, trade_dates)
    time.sleep(3)

    # Step 4: 全市场每日指标
    cache_market_daily_basic(pro, trade_dates)
    time.sleep(3)

    # Step 5: 大盘指数K线
    cache_index_kline(pro, start_date, end_date)
    time.sleep(3)

    # Step 6: 热门个股K线（回测/选股用）
    cache_stock_klines(pro, trade_dates, kline_days=args.kline,
                       top_n=args.topn, max_workers=args.workers)

    # 完成统计
    elapsed = time.time() - t0
    logger.info("=" * 50)
    logger.info(f"🎉 预缓存完成！耗时 {elapsed/60:.1f} 分钟")
    print_stats()
    logger.info("=" * 50)
    logger.info("现在可以运行: python -m BBBIG.main select")


if __name__ == "__main__":
    main()
