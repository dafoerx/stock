"""
backtest/runner.py — 回测执行器
统一封装 backtesting.py 的 Backtest 调用，支持单股/批量/参数优化
"""

import json
from pathlib import Path
from datetime import datetime

import pandas as pd
from backtesting import Backtest

from .loader import load_stock, load_stocks

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def run_single(
    ts_code: str,
    strategy_cls,
    start_date: str = None,
    end_date: str = None,
    cash: float = 100_000,
    commission: float = 0.0015,
    db_path: str = None,
    verbose: bool = True,
) -> dict:
    """
    对单只股票运行回测。

    Parameters
    ----------
    ts_code      : 股票代码，如 '600519.SH'
    strategy_cls : backtesting.Strategy 子类
    start_date   : 'YYYYMMDD'
    end_date     : 'YYYYMMDD'
    cash         : 初始资金
    commission   : 手续费率（双边，默认 0.15%）
    verbose      : 是否打印结果摘要

    Returns
    -------
    dict  包含 stats + 配置信息
    """
    df = load_stock(ts_code, start_date, end_date, db_path)
    bt = Backtest(df, strategy_cls, cash=cash, commission=commission,
                  exclusive_orders=True, finalize_trades=True)
    stats = bt.run()

    result = {
        "ts_code": ts_code,
        "strategy": strategy_cls.__name__,
        "start_date": start_date,
        "end_date": end_date,
        "data_rows": len(df),
        "return_pct": round(float(stats["Return [%]"]), 2),
        "buy_hold_pct": round(float(stats["Buy & Hold Return [%]"]), 2),
        "sharpe": round(float(stats["Sharpe Ratio"]), 3),
        "max_drawdown_pct": round(float(stats["Max. Drawdown [%]"]), 2),
        "win_rate_pct": round(float(stats["Win Rate [%]"]), 1),
        "num_trades": int(stats["# Trades"]),
        "profit_factor": round(float(stats.get("Profit Factor", 0) or 0), 2),
    }

    if verbose:
        print(f"\n{'='*50}")
        print(f"  {ts_code}  |  策略: {strategy_cls.__name__}")
        print(f"{'='*50}")
        print(f"  回测收益:   {result['return_pct']:+.2f}%")
        print(f"  买入持有:   {result['buy_hold_pct']:+.2f}%")
        print(f"  夏普比率:   {result['sharpe']:.3f}")
        print(f"  最大回撤:   {result['max_drawdown_pct']:.2f}%")
        print(f"  胜率:       {result['win_rate_pct']:.1f}%")
        print(f"  交易次数:   {result['num_trades']}")
        print(f"  盈亏比:     {result['profit_factor']:.2f}")
        print(f"{'='*50}")

    return result


def run_batch(
    ts_codes: list,
    strategy_cls,
    start_date: str = None,
    end_date: str = None,
    cash: float = 100_000,
    commission: float = 0.0015,
    db_path: str = None,
    save: bool = True,
) -> pd.DataFrame:
    """
    批量回测多只股票，返回汇总 DataFrame，可选保存 JSON

    Returns
    -------
    pd.DataFrame  按 return_pct 降序排列
    """
    results = []
    for code in ts_codes:
        try:
            r = run_single(code, strategy_cls, start_date, end_date, cash, commission, db_path, verbose=False)
            results.append(r)
            print(f"  ✓ {code}  收益 {r['return_pct']:+.2f}%  胜率 {r['win_rate_pct']:.0f}%  交易{r['num_trades']}次")
        except Exception as e:
            print(f"  ✗ {code}  跳过: {e}")

    df = pd.DataFrame(results).sort_values("return_pct", ascending=False).reset_index(drop=True)

    if save and not df.empty:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = RESULTS_DIR / f"batch_{strategy_cls.__name__}_{ts}.json"
        df.to_json(out_path, orient="records", force_ascii=False, indent=2)
        print(f"\n结果已保存: {out_path}")

    return df


def optimize(
    ts_code: str,
    strategy_cls,
    param_grid: dict,
    start_date: str = None,
    end_date: str = None,
    maximize: str = "Return [%]",
    cash: float = 100_000,
    commission: float = 0.0015,
    db_path: str = None,
) -> tuple:
    """
    参数网格搜索优化。

    Parameters
    ----------
    param_grid : dict  参数搜索空间，如 {'fast': range(5,20), 'slow': range(20,60)}
    maximize   : str   优化目标指标名

    Returns
    -------
    (best_stats, best_params)
    """
    df = load_stock(ts_code, start_date, end_date, db_path)
    bt = Backtest(df, strategy_cls, cash=cash, commission=commission,
                  exclusive_orders=True, finalize_trades=True)
    stats, heatmap = bt.optimize(
        **param_grid,
        maximize=maximize,
        return_heatmap=True,
    )
    best_params = {k: getattr(stats._strategy, k) for k in param_grid}
    print(f"\n最优参数: {best_params}")
    print(f"最优{maximize}: {stats[maximize]:.2f}")
    return stats, best_params
