"""
策略3：RSI 超买超卖
RSI 低于 oversold 买入，高于 overbought 卖出
"""
import pandas as pd
from backtesting import Strategy


def RSI(prices, n=14):
    delta = pd.Series(prices).diff()
    gain  = delta.clip(lower=0).rolling(n).mean()
    loss  = (-delta.clip(upper=0)).rolling(n).mean()
    rs    = gain / loss.replace(0, float('inf'))
    return 100 - 100 / (1 + rs)


class RsiStrategy(Strategy):
    """
    RSI 超买超卖策略
    参数：
        period     : RSI 周期（默认14）
        oversold   : 超卖阈值，低于此值买入（默认30）
        overbought : 超买阈值，高于此值卖出（默认70）
    """
    period     = 14
    oversold   = 30
    overbought = 70

    def init(self):
        self.rsi = self.I(RSI, self.data.Close, self.period)

    def next(self):
        if self.rsi[-1] < self.oversold and not self.position:
            self.buy()
        elif self.rsi[-1] > self.overbought and self.position:
            self.sell()
