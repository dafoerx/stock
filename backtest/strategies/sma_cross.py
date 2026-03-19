"""
策略1：双均线金叉死叉
fast 均线上穿 slow 均线买入，下穿卖出
"""
from backtesting import Strategy
from backtesting.lib import crossover
import pandas as pd


def SMA(values, n):
    return pd.Series(values).rolling(n).mean()


class SmaCross(Strategy):
    """
    双均线策略（SMA Cross）
    参数：
        fast  : 快线周期（默认5）
        slow  : 慢线周期（默认20）
    """
    fast = 5
    slow = 20

    def init(self):
        close = self.data.Close
        self.sma_fast = self.I(SMA, close, self.fast)
        self.sma_slow = self.I(SMA, close, self.slow)

    def next(self):
        if crossover(self.sma_fast, self.sma_slow):
            self.buy()
        elif crossover(self.sma_slow, self.sma_fast):
            self.sell()
