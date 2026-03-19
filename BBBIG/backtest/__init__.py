"""
BBBIG/backtest/__init__.py
BBBIG 选股结果的回测验证入口
"""
from .bbbig_backtest import run_selection_backtest, run_latest_selection

__all__ = ["run_selection_backtest", "run_latest_selection"]
