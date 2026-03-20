"""
策略2：MACD 金叉死叉
DIF 上穿 DEA 买入，下穿平仓
"""
import pandas as pd
from backtesting import Strategy
from backtesting.lib import crossover


def EMA(values, n):
    return pd.Series(values).ewm(span=n, adjust=False).mean()


class MacdStrategy(Strategy):
    """
    MACD 策略
    参数：
        fast   : 快线 EMA 周期（默认12）
        slow   : 慢线 EMA 周期（默认26）
        signal : 信号线周期（默认9）
    """
    fast = 12
    slow = 26
    signal = 9

    def init(self):
        close = self.data.Close

        def macd_line(c, f, s):
            return EMA(c, f) - EMA(c, s)

        def signal_line(c, f, s, sig):
            diff = EMA(c, f) - EMA(c, s)
            return diff.ewm(span=sig, adjust=False).mean()

        self.dif = self.I(macd_line, close, self.fast, self.slow)
        self.dea = self.I(signal_line, close, self.fast, self.slow, self.signal)

    def next(self):
        if crossover(self.dif, self.dea) and not self.position:
            self.buy()
        elif crossover(self.dea, self.dif) and self.position:
            self.position.close()
