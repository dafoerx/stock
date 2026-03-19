"""
backtest/strategies/__init__.py
暴露所有内置策略，方便 from backtest.strategies import SmaCross
"""
from .sma_cross import SmaCross
from .macd_strategy import MacdStrategy
from .rsi_strategy import RsiStrategy

__all__ = ["SmaCross", "MacdStrategy", "RsiStrategy"]
