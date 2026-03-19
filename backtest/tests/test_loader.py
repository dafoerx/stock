"""
backtest/tests/test_loader.py — 验证数据加载和策略可用
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

def test_loader():
    from backtest.loader import load_stock, list_available_stocks
    stocks = list_available_stocks()
    assert len(stocks) > 0, "数据库为空"
    print(f"✓ 数据库中有 {len(stocks)} 只股票")

    # 取第一只有数据的股票测试
    code = stocks.iloc[0]["ts_code"]
    df = load_stock(code)
    assert len(df) > 0
    assert set(["Open","High","Low","Close","Volume"]).issubset(df.columns)
    print(f"✓ 加载 {code}  {len(df)} 条  {df.index[0].date()} ~ {df.index[-1].date()}")

def test_strategies():
    from backtest.loader import list_available_stocks
    from backtest.runner import run_single
    from backtest.strategies import SmaCross, MacdStrategy, RsiStrategy

    stocks = list_available_stocks()
    code = stocks[stocks["trading_days"] >= 60].iloc[0]["ts_code"]

    for strat in [SmaCross, MacdStrategy, RsiStrategy]:
        r = run_single(code, strat, verbose=False)
        assert "return_pct" in r
        print(f"✓ {strat.__name__}  {code}  收益 {r['return_pct']:+.2f}%  夏普 {r['sharpe']:.2f}")

if __name__ == "__main__":
    print("=== 数据加载测试 ===")
    test_loader()
    print("\n=== 策略运行测试 ===")
    test_strategies()
    print("\n✅ 全部通过")
