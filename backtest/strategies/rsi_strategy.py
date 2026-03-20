"""
策略3：RSI 超买超卖
RSI 低于 oversold 买入，高于 overbought 平仓
"""
import pandas as pd
from backtesting import Strategy


def RSI(prices, n=14):
    series = pd.Series(prices)
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()

    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.mask((avg_loss == 0) & (avg_gain > 0), 100)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss > 0), 0)
    rsi = rsi.mask((avg_gain == 0) & (avg_loss == 0), 50)
    return rsi.astype(float)


class RsiStrategy(Strategy):
    """
    RSI 超买超卖策略
    参数：
        period     : RSI 周期（默认14）
        oversold   : 超卖阈值，低于此值买入（默认30）
        overbought : 超买阈值，高于此值卖出（默认70）
    """
    period = 14
    oversold = 30
    overbought = 70

    def init(self):
        self.rsi = self.I(RSI, self.data.Close, self.period)

    def next(self):
        if self.rsi[-1] < self.oversold and not self.position:
            self.buy()
        elif self.rsi[-1] > self.overbought and self.position:
            self.position.close()
