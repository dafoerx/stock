"""
BBBIG/backtest/bbbig_backtest.py
将 BBBIG 选股推荐结果接入 backtesting.py 框架做真实历史回测

用法:
    # 指定选股结果文件
    python3 -m BBBIG.backtest.bbbig_backtest --file data/results/selection_20260319_093226.json

    # 自动读取最新一次选股结果
    python3 -m BBBIG.backtest.bbbig_backtest --latest
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd

# 路径修正：从 BBBIG/backtest/ 向上找到 stock/ 根目录
ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from backtest.loader import load_stock
from backtest.runner import run_single, run_batch
from backtest.strategies import SmaCross, MacdStrategy, RsiStrategy

RESULTS_DIR = Path(__file__).parent.parent / "data" / "results"
BBBIG_BACKTEST_DIR = Path(__file__).parent / "results"
BBBIG_BACKTEST_DIR.mkdir(exist_ok=True)


def load_selection_file(filepath: str) -> dict:
    """加载 BBBIG 选股结果 JSON"""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def get_latest_selection() -> Path:
    """找最新的 selection_*.json（排除 backtest_ 前缀）"""
    files = sorted(RESULTS_DIR.glob("selection_*.json"), reverse=True)
    if not files:
        raise FileNotFoundError(f"No selection files found in {RESULTS_DIR}")
    return files[0]


def run_selection_backtest(
    selection_data: dict,
    strategy_cls=None,
    lookback_days: int = 90,
    cash: float = 100_000,
    commission: float = 0.0015,
    save: bool = True,
) -> pd.DataFrame:
    """
    对 BBBIG 选股推荐列表做 backtesting.py 真实历史回测。

    Parameters
    ----------
    selection_data : dict   BBBIG 选股 JSON 数据
    strategy_cls   :        回测使用的策略类（默认 SmaCross）
    lookback_days  : int    回测历史窗口（默认90交易日）
    cash           : float  初始资金
    commission     : float  手续费率

    Returns
    -------
    pd.DataFrame  回测结果汇总，按收益率降序
    """
    if strategy_cls is None:
        strategy_cls = SmaCross

    recommendations = selection_data.get("recommendations", [])
    if not recommendations:
        raise ValueError("选股结果为空")

    # 确定回测日期范围
    sel_time = selection_data.get("timestamp", "")
    if sel_time:
        end_dt = datetime.strptime(sel_time[:10], "%Y-%m-%d")
    else:
        end_dt = datetime.today()
    start_dt = end_dt - timedelta(days=int(lookback_days * 1.5))  # 多取一些保证够用
    start_date = start_dt.strftime("%Y%m%d")
    end_date   = end_dt.strftime("%Y%m%d")

    print(f"\n{'='*60}")
    print(f"  BBBIG 选股回测  ({start_date} ~ {end_date})")
    print(f"  策略: {strategy_cls.__name__}  |  窗口: {lookback_days}日")
    print(f"  选股时间: {sel_time}")
    print(f"  大盘风险: {selection_data.get('market_risk', {}).get('risk_level', '-')}")
    print(f"{'='*60}")

    ts_codes = [r["code"] + (".SH" if r["code"].startswith("6") else ".SZ")
                for r in recommendations]
    names    = {r["code"] + (".SH" if r["code"].startswith("6") else ".SZ"): r["name"]
                for r in recommendations}
    ai_ranks = {r["code"] + (".SH" if r["code"].startswith("6") else ".SZ"): r["rank"]
                for r in recommendations}
    ai_wins  = {r["code"] + (".SH" if r["code"].startswith("6") else ".SZ"): r.get("backtest_win_rate", 0)
                for r in recommendations}

    results = []
    for code in ts_codes:
        name = names.get(code, "")
        try:
            r = run_single(
                code, strategy_cls, start_date, end_date,
                cash=cash, commission=commission, verbose=False
            )
            r["name"]       = name
            r["ai_rank"]    = ai_ranks.get(code, "-")
            r["ai_win_rate"] = ai_wins.get(code, 0)
            results.append(r)
            print(f"  ✓ #{r['ai_rank']:2d} {code} {name:<8s}  "
                  f"收益 {r['return_pct']:+6.2f}%  "
                  f"B&H {r['buy_hold_pct']:+6.2f}%  "
                  f"胜率 {r['win_rate_pct']:5.1f}%  "
                  f"夏普 {r['sharpe']:.2f}  "
                  f"AI胜率 {r['ai_win_rate']:.0f}%")
        except Exception as e:
            print(f"  ✗ {code} {name}  跳过: {e}")

    if not results:
        print("所有股票回测失败，无结果")
        return pd.DataFrame()

    df = pd.DataFrame(results)
    df = df.sort_values("return_pct", ascending=False).reset_index(drop=True)

    # 汇总
    print(f"\n{'='*60}")
    print(f"  汇总 (按策略收益降序)")
    print(f"  平均收益: {df['return_pct'].mean():+.2f}%")
    print(f"  平均夏普: {df['sharpe'].mean():.3f}")
    print(f"  平均最大回撤: {df['max_drawdown_pct'].mean():.2f}%")
    print(f"  策略胜过买入持有: {(df['return_pct'] > df['buy_hold_pct']).sum()}/{len(df)} 只")
    print(f"{'='*60}")

    if save:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = BBBIG_BACKTEST_DIR / f"bbbig_bt_{strategy_cls.__name__}_{ts}.json"
        df.to_json(out_path, orient="records", force_ascii=False, indent=2)
        print(f"\n结果已保存: {out_path}")

    return df


def run_latest_selection(strategy_cls=None, lookback_days: int = 90, **kwargs) -> pd.DataFrame:
    """自动读取最新选股结果并回测"""
    latest = get_latest_selection()
    print(f"读取最新选股: {latest.name}")
    data = load_selection_file(latest)
    return run_selection_backtest(data, strategy_cls, lookback_days, **kwargs)


def compare_strategies(selection_data: dict, lookback_days: int = 90) -> pd.DataFrame:
    """
    对同一批选股结果，用多个策略分别回测，横向对比
    """
    strategies = [SmaCross, MacdStrategy, RsiStrategy]
    all_results = []

    for strat in strategies:
        print(f"\n\n>>> 策略: {strat.__name__}")
        df = run_selection_backtest(selection_data, strat, lookback_days, save=False)
        if not df.empty:
            df["strategy_name"] = strat.__name__
            all_results.append(df)

    if not all_results:
        return pd.DataFrame()

    combined = pd.concat(all_results, ignore_index=True)

    print(f"\n\n{'='*60}")
    print("  多策略横向对比（平均收益）")
    print(f"{'='*60}")
    summary = combined.groupby("strategy_name")["return_pct"].agg(["mean", "std", "min", "max"])
    print(summary.to_string())

    return combined


# ─── CLI 入口 ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BBBIG 选股回测")
    parser.add_argument("--file",     type=str, help="指定选股 JSON 文件路径")
    parser.add_argument("--latest",   action="store_true", help="自动使用最新选股结果")
    parser.add_argument("--strategy", type=str, default="SmaCross",
                        choices=["SmaCross", "MacdStrategy", "RsiStrategy"],
                        help="回测策略 (默认 SmaCross)")
    parser.add_argument("--compare",  action="store_true", help="多策略横向对比")
    parser.add_argument("--days",     type=int, default=90, help="回测历史窗口（默认90）")
    args = parser.parse_args()

    strategy_map = {
        "SmaCross":    SmaCross,
        "MacdStrategy": MacdStrategy,
        "RsiStrategy": RsiStrategy,
    }
    strat_cls = strategy_map[args.strategy]

    if args.file:
        data = load_selection_file(args.file)
    elif args.latest:
        data = load_selection_file(get_latest_selection())
    else:
        parser.print_help()
        sys.exit(1)

    if args.compare:
        compare_strategies(data, args.days)
    else:
        run_selection_backtest(data, strat_cls, args.days)
