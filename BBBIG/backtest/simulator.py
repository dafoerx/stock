#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BBBIG 策略回测模拟器
模拟真实资金管理流程：
  - 以N周前为起点，按周重新运行完整选股流程
  - 按系统推荐仓位和大盘风险等级分配资金
  - 买入条件：次日价格落入推荐买入区间才成交，否则换购备选股
  - 卖出条件：目标价/止损价先触达者成交（判断当日先后顺序）
  - 计算手续费（买入0.03% + 卖出0.03% + 印花税0.1%）
  - 输出：每周净值、最终收益率、持仓明细、交易记录
"""
import logging
import os
import re
import json
import random
import time
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from BBBIG.data_fetcher import fetcher
from BBBIG.config import DB_FILE, RESULT_DIR, KLINE_DAYS, SIM_MAX_BUY_RANK, STOP_LOSS_MODE

logger = logging.getLogger("BBBIG")

# ========== 手续费常数 ==========
BUY_COMMISSION  = 0.0003   # 买入佣金 0.03%
SELL_COMMISSION = 0.0003   # 卖出佣金 0.03%
STAMP_DUTY      = 0.001    # 印花税  0.10%（仅卖出）
MIN_COMMISSION  = 5.0      # 最低佣金（元）


# ========== 工具函数 ==========

def _get_trade_dates_from_db(start_date: str, end_date: str) -> list:
    """从 trade_cal 表获取指定范围内的交易日列表（YYYYMMDD 字符串）"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute(
            "SELECT cal_date FROM trade_cal WHERE cal_date >= ? AND cal_date <= ? "
            "AND is_open = 1 ORDER BY cal_date ASC",
            (start_date, end_date)
        )
        rows = cur.fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        logger.warning(f"获取交易日历失败: {e}")
        # fallback: 简单跳周末
        result = []
        d = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")
        while d <= end:
            if d.weekday() < 5:
                result.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)
        return result


def _normalize_ts_code(code: str) -> str:
    code = (code or "").strip().upper()
    if not code:
        return code
    if '.' in code:
        return code
    if code.startswith(('5', '6', '9')):
        return f"{code}.SH"
    return f"{code}.SZ"


def _nearest_trade_date_before(date_str: str) -> str:
    """返回不晚于 date_str 的最近交易日（含当日）"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute(
            "SELECT cal_date FROM trade_cal WHERE cal_date <= ? AND is_open = 1 "
            "ORDER BY cal_date DESC LIMIT 1",
            (date_str,)
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception:
        pass
    # fallback
    d = datetime.strptime(date_str, "%Y%m%d")
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def _next_trade_date(date_str: str) -> str:
    """返回 date_str 之后的第一个交易日"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute(
            "SELECT cal_date FROM trade_cal WHERE cal_date > ? AND is_open = 1 "
            "ORDER BY cal_date ASC LIMIT 1",
            (date_str,)
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception:
        pass
    d = datetime.strptime(date_str, "%Y%m%d") + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y%m%d")


def _parse_buy_range(val) -> tuple:
    """解析买入区间字符串，返回 (low, high)"""
    if isinstance(val, str):
        nums = re.findall(r"[\d.]+", val)
        if len(nums) >= 2:
            return float(nums[0]), float(nums[1])
        elif len(nums) == 1:
            p = float(nums[0])
            return p * 0.98, p * 1.02
    if isinstance(val, (int, float)):
        p = float(val)
        return p * 0.98, p * 1.02
    return 0.0, 0.0


def _parse_price(val) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        nums = re.findall(r"[\d.]+", val)
        if nums:
            return float(nums[0])
    return 0.0


def _calc_buy_cost(price: float, shares: int) -> float:
    """计算买入总成本（含佣金）"""
    gross = price * shares
    fee = max(gross * BUY_COMMISSION, MIN_COMMISSION)
    return round(gross + fee, 2)


def _calc_sell_proceeds(price: float, shares: int) -> float:
    """计算卖出实收金额（扣佣金+印花税）"""
    gross = price * shares
    fee = max(gross * SELL_COMMISSION, MIN_COMMISSION) + gross * STAMP_DUTY
    return round(gross - fee, 2)


def _get_kline_for_date_range(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    获取指定日期范围内的K线数据。
    优先从 SQLite 缓存读取，再 fallback 到 Tushare API。
    返回的 DataFrame 含列: 日期(str YYYYMMDD), 开盘, 最高, 最低, 收盘, 成交量
    日期升序排列。
    """
    try:
        conn = sqlite3.connect(DB_FILE)
        df = pd.read_sql_query(
            "SELECT trade_date, open, high, low, close, vol "
            "FROM daily WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ? "
            "ORDER BY trade_date ASC",
            conn,
            params=(_normalize_ts_code(code), start_date, end_date)
        )
        conn.close()
        if not df.empty:
            df.columns = ["日期", "开盘", "最高", "最低", "收盘", "成交量"]
            return df
    except Exception as e:
        logger.debug(f"SQLite查询失败({code}): {e}")

    # fallback: 用 fetcher
    days_needed = (datetime.strptime(end_date, "%Y%m%d") -
                   datetime.strptime(start_date, "%Y%m%d")).days + 30
    try:
        df = fetcher.fetch_stock_kline(code, days=days_needed, end_date_str=end_date)
        if not df.empty:
            sd_dash = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
            df = df[df["日期"] >= sd_dash].reset_index(drop=True)
        return df
    except Exception as e:
        logger.warning(f"获取K线失败({code}): {e}")
        return pd.DataFrame()


# ========== 持仓记录 ==========

class Position:
    """单只股票持仓"""
    def __init__(self, code: str, name: str, industry: str,
                 buy_date: str, buy_price: float, shares: int,
                 target_price: float, stop_loss: float,
                 cost_total: float):
        self.code = code
        self.name = name
        self.industry = industry
        self.buy_date = buy_date
        self.buy_price = buy_price
        self.shares = shares
        self.target_price = target_price
        self.stop_loss = stop_loss
        self.cost_total = cost_total          # 含佣金的总成本
        self.current_price = buy_price        # 盯市价格
        self.sell_date: Optional[str] = None
        self.sell_price: Optional[float] = None
        self.proceeds: Optional[float] = None  # 卖出实收
        self.status = "持有"                   # 持有 / 盈利卖出 / 止损卖出 / 持有到期

    @property
    def market_value(self) -> float:
        return self.current_price * self.shares

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_total

    @property
    def realized_pnl(self) -> float:
        if self.proceeds is not None:
            return self.proceeds - self.cost_total
        return 0.0

    @property
    def floating_pnl(self) -> float:
        """持仓浮动盈亏（未平仓时有效）"""
        if self.proceeds is None:
            return round(self.market_value - self.cost_total, 2)
        return 0.0

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "industry": self.industry,
            "buy_date": self.buy_date, "buy_price": self.buy_price,
            "shares": self.shares, "target_price": self.target_price,
            "stop_loss": self.stop_loss, "cost_total": self.cost_total,
            "current_price": self.current_price,
            "sell_date": self.sell_date, "sell_price": self.sell_price,
            "proceeds": self.proceeds, "status": self.status,
            # Bug4修复：持仓中显示浮动盈亏，已平仓显示实现盈亏
            "pnl": round(self.floating_pnl if self.proceeds is None else self.realized_pnl, 2),
            "pnl_type": "浮动" if self.proceeds is None else "实现",
        }


# ========== 模拟器核心 ==========

class BBBIGSimulator:
    """
    BBBIG 完整策略回测模拟器

    用法:
        sim = BBBIGSimulator(initial_capital=30000, sim_weeks=4)
        report = sim.run()
        print(sim.format_report(report))
    """

    def __init__(self, initial_capital: float = 30000.0, sim_weeks: int = 4,
                 quant_mode: bool = False):
        """
        :param initial_capital: 初始资金（元）
        :param sim_weeks: 回测总周数（从多少周前开始）
        :param quant_mode: 是否使用纯量化选股模式（不调AI，用多因子评分替代）
        """
        self.initial_capital = initial_capital
        self.sim_weeks = sim_weeks
        self.quant_mode = quant_mode

        self.cash = initial_capital          # 当前可用资金
        self.positions: dict[str, Position] = {}   # code -> Position（持有中）
        self.closed_positions: list[Position] = []  # 已平仓
        self.trade_log: list[dict] = []
        self.nav_history: list[dict] = []          # 每周净值快照

        # 大盘风险 → 总仓位上限
        self._risk_to_position = {"低": 0.80, "中": 0.45, "高": 0.30}

        # 个股止损冷静期：止损后N个交易日内不再买入同一只
        self._stop_loss_dates: dict[str, str] = {}
        self.COOLDOWN_DAYS = 10

        # 行业止损冷静期：同行业止损后N个交易日内不再买入该行业
        self._industry_stop_loss_dates: dict[str, str] = {}  # industry -> last stop_loss date
        self.INDUSTRY_COOLDOWN_DAYS = 5  # 行业冷静期（比个股短一些）

        # 策略级风控：止损后暂停补仓，且单周连续止损达到阈值后停止当周补仓
        self.POST_STOP_LOSS_REFILL_COOLDOWN = 3   # 跳过3个完整交易日后再允许补仓（加长：避免连续踩雷）
        self.WEEKLY_STOP_LOSS_REFILL_LIMIT = 2    # 单周累计止损达到2笔后停止当周补仓
        self._weekly_stop_loss_count = 0
        self._weekly_refill_blocked = False

        # 全局熔断：连续N笔止损后，整体暂停建仓/补仓1周
        self.GLOBAL_STOP_LOSS_STREAK_LIMIT = 3   # 连续止损达到3笔触发全局熔断
        self._global_stop_loss_streak = 0         # 当前连续止损计数（盈利卖出重置）
        self._global_circuit_breaker = False       # 全局熔断标志
        self._circuit_breaker_weeks_left = 0       # 熔断剩余周数

        self.MAX_BUY_RANK = SIM_MAX_BUY_RANK
        self.stop_loss_mode = STOP_LOSS_MODE

        # 跨日/跨周补仓：卖出后的待补仓选股结果
        self._pending_refill_recs: Optional[list] = None
        self._pending_refill_budget: float = 0.0
        self._pending_refill_buy_after: Optional[str] = None
        self._pending_refill_reason: str = ""

    # -------- 净值计算 --------

    def _total_assets(self) -> float:
        mv = sum(p.market_value for p in self.positions.values())
        return round(self.cash + mv, 2)

    def _nav_snapshot(self, date_str: str, label: str = "") -> dict:
        total = self._total_assets()
        ret = (total - self.initial_capital) / self.initial_capital * 100
        snap = {
            "date": date_str,
            "label": label,
            "cash": round(self.cash, 2),
            "market_value": round(sum(p.market_value for p in self.positions.values()), 2),
            "total_assets": total,
            "return_pct": round(ret, 2),
            "position_count": len(self.positions),
        }
        self.nav_history.append(snap)
        return snap

    # -------- 大盘风险 → 仓位系数 --------

    def _position_ratio(self, risk_level: str) -> float:
        return self._risk_to_position.get(risk_level, 0.45)

    def _trade_date_after(self, date_str: str, steps: int = 1) -> str:
        result = date_str
        for _ in range(max(steps, 0)):
            result = _next_trade_date(result)
        return result

    def _clear_pending_refill(self):
        self._pending_refill_recs = None
        self._pending_refill_budget = 0.0
        self._pending_refill_buy_after = None
        self._pending_refill_reason = ""

    def _schedule_refill(self, recs: list, budget: float, exit_date: str,
                         cooldown_days: int, reason: str):
        self._pending_refill_recs = recs
        self._pending_refill_budget = budget
        self._pending_refill_buy_after = self._trade_date_after(exit_date, cooldown_days + 1)
        self._pending_refill_reason = reason
        logger.info(f"  [{exit_date}] 补仓候选 {len(recs)} 只，{reason}，"
                    f"最早 {self._pending_refill_buy_after} 买入")

    def _process_pending_refill(self, trade_date: str):
        if not self._pending_refill_recs:
            return

        buy_after = self._pending_refill_buy_after or trade_date
        if trade_date < buy_after:
            logger.info(f"  [{trade_date}] 补仓冷静期中，最早 {buy_after} 再尝试买入")
            return

        reason = self._pending_refill_reason or "基于前次卖出后选股"
        logger.info(f"  [{trade_date}] 补仓买入（{reason}）")
        self._buy_from_selections(
            self._pending_refill_recs, trade_date,
            self._pending_refill_budget)
        self._clear_pending_refill()

    # -------- 每日盯市 + 卖出检查 --------

    def _mark_to_market(self, date_str: str):
        """更新当日收盘价（盯市）"""
        for code, pos in list(self.positions.items()):
            kline = _get_kline_for_date_range(code, date_str, date_str)
            if not kline.empty:
                pos.current_price = float(kline.iloc[-1]["收盘"])

    def _check_exits(self, date_str: str) -> Tuple[list, list]:
        """
        检查当日是否触达目标价或止损价。
        目标价仍按日内高点触发；止损支持收盘确认，避免被盘中下影线轻易洗出。
        返回 (已平仓code列表, 止损卖出code列表)。
        """
        exited = []
        stop_loss_exited = []
        for code, pos in list(self.positions.items()):
            kline = _get_kline_for_date_range(code, date_str, date_str)
            if kline.empty:
                continue
            row = kline.iloc[0]
            high = float(row["最高"])
            low = float(row["最低"])
            close = float(row["收盘"])
            pos.current_price = close

            hit_target = (pos.target_price > 0) and (high >= pos.target_price)
            if self.stop_loss_mode == "close_confirmed":
                hit_stop = (pos.stop_loss > 0) and (close <= pos.stop_loss)
            else:
                hit_stop = (pos.stop_loss > 0) and (low <= pos.stop_loss)

            if not hit_target and not hit_stop:
                continue

            if hit_target:
                sell_price = pos.target_price
                pos.status = "盈利卖出"
            elif self.stop_loss_mode == "close_confirmed":
                sell_price = close
                pos.status = "止损卖出（收盘确认）"
            else:
                sell_price = pos.stop_loss
                pos.status = "止损卖出"

            proceeds = _calc_sell_proceeds(sell_price, pos.shares)
            pos.sell_date  = date_str
            pos.sell_price = sell_price
            pos.proceeds   = proceeds
            self.cash += proceeds

            self.trade_log.append({
                "action": "SELL", "code": code, "name": pos.name,
                "date": date_str, "price": sell_price, "shares": pos.shares,
                "proceeds": proceeds, "pnl": round(proceeds - pos.cost_total, 2),
                "status": pos.status,
            })
            logger.info(f"  [{date_str}] 卖出 {code} {pos.name} @ {sell_price:.2f} "
                        f"→ {pos.status}，实现盈亏 {proceeds - pos.cost_total:+.2f}元")

            self.closed_positions.append(pos)
            del self.positions[code]
            exited.append(code)

            if pos.status.startswith("止损卖出"):
                stop_loss_exited.append(code)
                self._stop_loss_dates[code] = date_str
                # 记录行业止损冷静期
                if pos.industry:
                    self._industry_stop_loss_dates[pos.industry] = date_str
                    logger.info(f"  [{date_str}] 行业[{pos.industry}]进入冷静期"
                                f"（{self.INDUSTRY_COOLDOWN_DAYS}个交易日）")
                # 全局连续止损计数
                self._global_stop_loss_streak += 1
                if self._global_stop_loss_streak >= self.GLOBAL_STOP_LOSS_STREAK_LIMIT:
                    self._global_circuit_breaker = True
                    self._circuit_breaker_weeks_left = 1
                    logger.info(f"  [{date_str}] ⚠️ 全局熔断触发！连续止损"
                                f"{self._global_stop_loss_streak}笔，暂停建仓1周")
                logger.info(f"  [{date_str}] {code} 进入止损冷静期（{self.COOLDOWN_DAYS}个交易日）"
                            f"| 全局连续止损 {self._global_stop_loss_streak} 笔")
            else:
                # 盈利卖出，重置全局连续止损计数
                if self._global_stop_loss_streak > 0:
                    logger.info(f"  [{date_str}] 盈利卖出，全局连续止损计数"
                                f"从{self._global_stop_loss_streak}重置为0")
                self._global_stop_loss_streak = 0

        return exited, stop_loss_exited

    # -------- 买入逻辑 --------

    def _try_buy(self, rec: dict, buy_date: str,
                 total_budget: float, remaining_budget: float) -> Tuple[bool, float]:
        """
        尝试在 buy_date 以推荐买入区间成交。
        当日实际价格（开盘~收盘范围）与区间有交集才成交。
        成交价取 min(buy_high, 当日最高价) 与 max(buy_low, 当日最低价) 的中值。
        :return: (是否成交, 实际占用预算)
        """
        code = rec.get("code", "")
        name = rec.get("name", code)
        if code in self.positions:
            return False, 0.0  # 已持有

        # Bug2修复：检查止损冷静期
        if code in self._stop_loss_dates:
            sl_date = self._stop_loss_dates[code]
            # 获取止损日之后的交易日数
            all_dates_after = _get_trade_dates_from_db(sl_date, buy_date)
            days_since = len(all_dates_after) - 1  # 不含止损日本身
            if days_since < self.COOLDOWN_DAYS:
                logger.info(f"  [{buy_date}] {code} {name} 止损冷静期中（已{days_since}天，需{self.COOLDOWN_DAYS}天），跳过")
                return False, 0.0

        # 行业止损冷静期检查
        rec_industry = rec.get("industry", "")
        if rec_industry and rec_industry in self._industry_stop_loss_dates:
            ind_sl_date = self._industry_stop_loss_dates[rec_industry]
            all_dates_after = _get_trade_dates_from_db(ind_sl_date, buy_date)
            days_since = len(all_dates_after) - 1
            if days_since < self.INDUSTRY_COOLDOWN_DAYS:
                logger.info(f"  [{buy_date}] {code} {name} 行业[{rec_industry}]冷静期中"
                            f"（已{days_since}天，需{self.INDUSTRY_COOLDOWN_DAYS}天），跳过")
                return False, 0.0

        buy_low, buy_high = _parse_buy_range(rec.get("suggested_buy_range", ""))
        if buy_low <= 0 or buy_high <= 0:
            logger.debug(f"  {code} 买入区间无效，跳过")
            return False, 0.0

        target = _parse_price(rec.get("target_price", 0))
        stop   = _parse_price(rec.get("stop_loss", 0))

        kline = _get_kline_for_date_range(code, buy_date, buy_date)
        if kline.empty:
            logger.debug(f"  {code} {buy_date} 无K线数据，跳过")
            return False, 0.0

        row = kline.iloc[0]
        day_low  = float(row["最低"])
        day_high = float(row["最高"])

        # 判断价格区间是否与当日K线有交集
        if day_high < buy_low or day_low > buy_high:
            logger.info(f"  [{buy_date}] {code} {name} 价格 [{day_low:.2f},{day_high:.2f}] "
                        f"未落入买入区间 [{buy_low:.2f},{buy_high:.2f}]，跳过")
            return False, 0.0

        # ---------- 趋势确认：收盘站上5日均线 + 当日非大阴线 ----------
        day_open  = float(row["开盘"])
        day_close = float(row["收盘"])
        # 获取前10个自然日（覆盖5个交易日）的K线用于计算5日均线
        try:
            _ma_start_dt = datetime.strptime(buy_date, "%Y%m%d") - timedelta(days=12)
            _ma_start = _ma_start_dt.strftime("%Y%m%d")
            _hist = _get_kline_for_date_range(code, _ma_start, buy_date)
            if _hist is not None and len(_hist) >= 5:
                _recent5 = _hist.tail(5)
                ma5 = float(_recent5["收盘"].mean())
                # 条件1: 收盘价须 >= 5日均线的98%（留一点容差）
                if day_close < ma5 * 0.98:
                    logger.info(f"  [{buy_date}] {code} {name} 收盘{day_close:.2f} < "
                                f"MA5*0.98={ma5*0.98:.2f}，趋势偏弱，跳过")
                    return False, 0.0
                # 条件2: 当日不能是大阴线（跌幅超过 -2%）
                if day_open > 0 and (day_close - day_open) / day_open < -0.02:
                    logger.info(f"  [{buy_date}] {code} {name} 当日大阴线"
                                f"（{(day_close-day_open)/day_open*100:.1f}%），跳过")
                    return False, 0.0
        except Exception as _e:
            logger.debug(f"  [{buy_date}] {code} 趋势确认异常: {_e}，继续买入")

        # 成交价：区间与当日K线的交集中点
        actual_low  = max(buy_low,  day_low)
        actual_high = min(buy_high, day_high)
        exec_price  = round((actual_low + actual_high) / 2, 3)

        budget_cap = min(max(remaining_budget, 0.0), self.cash)
        if budget_cap <= 0:
            logger.info(f"  [{buy_date}] {code} {name} 无剩余预算，跳过")
            return False, 0.0

        # 按推荐仓位比例计算资金（兼容多种字段名）
        raw_pos = rec.get("position_weight",
                          rec.get("position_pct",
                                  rec.get("position", "8%")))
        # 解析百分比字符串或数字
        if isinstance(raw_pos, str):
            nums = re.findall(r"[\d.]+", raw_pos)
            rec_position_pct = float(nums[0]) if nums else 8.0
        else:
            rec_position_pct = float(raw_pos)
        if rec_position_pct > 1:
            rec_position_pct /= 100  # 兼容 "15"/"15%" 和 "0.15" 格式

        target_cash = total_budget * rec_position_pct
        alloc_cash = min(target_cash, budget_cap)
        min_lot_cost = _calc_buy_cost(exec_price, 100)
        if alloc_cash < min_lot_cost:
            if budget_cap < min_lot_cost:
                logger.info(f"  [{buy_date}] {code} {name} 剩余预算 {budget_cap:.2f}元，不足买入1手，跳过")
                return False, 0.0
            alloc_cash = min_lot_cost

        shares = int(alloc_cash / exec_price / 100) * 100  # 取整到100股
        while shares > 0 and _calc_buy_cost(exec_price, shares) > budget_cap:
            shares -= 100
        if shares <= 0:
            logger.info(f"  [{buy_date}] {code} {name} 剩余预算 {budget_cap:.2f}元，无法买入整手")
            return False, 0.0

        cost = _calc_buy_cost(exec_price, shares)
        self.cash -= cost
        pos = Position(
            code=code, name=name,
            industry=rec.get("industry", ""),
            buy_date=buy_date, buy_price=exec_price, shares=shares,
            target_price=target, stop_loss=stop, cost_total=cost,
        )
        self.positions[code] = pos

        self.trade_log.append({
            "action": "BUY", "code": code, "name": name,
            "date": buy_date, "price": exec_price, "shares": shares,
            "cost": cost, "target": target, "stop": stop,
        })
        logger.info(f"  [{buy_date}] 买入 {code} {name} @ {exec_price:.2f} × {shares}股 "
                    f"= {cost:.2f}元 | 目标{target:.2f} 止损{stop:.2f} | "
                    f"本轮预算剩余 {max(remaining_budget - cost, 0.0):.2f}元")
        return True, cost

    # -------- 每周选股 + 建仓 --------

    def _run_weekly_selection(self, selection_date: str, risk_level: str) -> list:
        """
        在 selection_date 重新运行完整选股。
        quant_mode=True 时使用纯量化多因子选股（不需要AI）。
        通过设置环境变量 BBBIG_BACKTEST_DATE 让 fetcher 的缓存查询以该日期为基准。
        返回推荐股列表。
        """
        prev_backtest_date = os.environ.get("BBBIG_BACKTEST_DATE")
        os.environ["BBBIG_BACKTEST_DATE"] = selection_date
        try:
            if self.quant_mode:
                return self._run_quant_selection(selection_date, risk_level)
            else:
                return self._run_ai_selection(selection_date, risk_level)
        finally:
            if prev_backtest_date is None:
                os.environ.pop("BBBIG_BACKTEST_DATE", None)
            else:
                os.environ["BBBIG_BACKTEST_DATE"] = prev_backtest_date

    def _run_ai_selection(self, selection_date: str, risk_level: str) -> list:
        """AI选股模式（需要 DeepSeek API）"""
        from BBBIG.stock_selector import run_stock_selection
        logger.info(f"  [选股] 模拟 {selection_date} 的选股流程（真实AI选股）...")
        try:
            result = run_stock_selection(selection_date=selection_date)
            recs = result.get("recommendations", [])
            market_risk = result.get("market_risk", {})
            if market_risk:
                risk_level = market_risk.get("risk_level", risk_level)
            logger.info(f"  [选股] 共选出 {len(recs)} 只股票，大盘风险={risk_level}")
            return recs, risk_level
        except Exception as e:
            logger.error(f"  [选股] 选股失败: {e}")
            return [], risk_level

    def _run_quant_selection(self, selection_date: str, risk_level: str) -> list:
        """纯量化选股模式（不需要AI，用多因子评分）"""
        from BBBIG.backtester import _quant_select_at_date
        logger.info(f"  [选股] 模拟 {selection_date} 的选股流程（纯量化模式）...")
        try:
            recs = _quant_select_at_date(selection_date, top_n=10)
            # 为每只候选分配仓位权重
            for i, rec in enumerate(recs):
                rec["rank"] = i + 1
                rec["position_weight"] = "10%"  # 均分仓位
            # 纯量化模式下通过大盘行情判断风险等级
            risk_level = self._estimate_market_risk_quant(selection_date, risk_level)
            logger.info(f"  [选股] 共选出 {len(recs)} 只股票，大盘风险={risk_level}")
            return recs, risk_level
        except Exception as e:
            logger.error(f"  [选股] 纯量化选股失败: {e}")
            import traceback
            traceback.print_exc()
            return [], risk_level

    def _estimate_market_risk_quant(self, date_str: str, fallback_risk: str) -> str:
        """纯量化模式下，用沪深300指数估算大盘风险等级"""
        try:
            # 获取沪深300指数近20日数据
            start_dt = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=40)
            start_str = start_dt.strftime("%Y%m%d")
            kline = _get_kline_for_date_range("399300.SZ", start_str, date_str)
            if kline is None or kline.empty:
                # 尝试上证指数
                kline = _get_kline_for_date_range("000001.SH", start_str, date_str)
            if kline is None or len(kline) < 10:
                return fallback_risk
            recent = kline.tail(20)
            close_5d = kline.tail(5)["收盘"]
            change_5d = (close_5d.iloc[-1] - close_5d.iloc[0]) / close_5d.iloc[0] * 100
            ma20 = recent["收盘"].mean()
            latest_close = float(kline.iloc[-1]["收盘"])
            if change_5d < -3.0 or latest_close < ma20 * 0.97:
                return "高"
            elif change_5d < -1.0 or latest_close < ma20:
                return "中"
            else:
                return "低"
        except Exception as e:
            logger.debug(f"  大盘风险估算失败: {e}")
            return fallback_risk

    def _buy_from_selections(self, recs: list, buy_date: str, total_budget: float):
        """
        仅对回测排序靠前的候选尝试建仓，避免后排股票因为更容易跌入区间而被动成交。
        若某只未成交（价格未入区间），自动顺延尝试下一只备选。
        每次成交后实时扣减本轮预算，避免多只股票重复占用同一笔可建仓资金。
        """
        if not recs or total_budget <= 0:
            return

        ranked_recs = sorted(
            recs,
            key=lambda r: int(r.get("rank", 10**9)) if str(r.get("rank", "")).isdigit() else 10**9
        )
        tradable_recs = ranked_recs[:self.MAX_BUY_RANK] if self.MAX_BUY_RANK > 0 else ranked_recs
        remaining_budget = min(total_budget, self.cash)
        logger.info(f"  [{buy_date}] 仅尝试前{self.MAX_BUY_RANK}名候选建仓（本次候选 {len(tradable_recs)}/{len(recs)}），"
                    f"初始预算 {remaining_budget:.2f}元")

        bought = 0
        for rec in tradable_recs:
            code = rec.get("code", "")
            if code in self.positions:
                continue
            if remaining_budget <= 0 or self.cash <= 0:
                logger.info(f"  [{buy_date}] 本轮预算已用尽，停止建仓")
                break
            ok, spent = self._try_buy(rec, buy_date, total_budget, remaining_budget)
            if ok:
                bought += 1
                remaining_budget = max(0.0, remaining_budget - spent)

        used_budget = max(0.0, total_budget - remaining_budget)
        logger.info(f"  [{buy_date}] 本周建仓 {bought} 只，已用预算 {used_budget:.2f}元 / {total_budget:.2f}元，"
                    f"持仓 {len(self.positions)} 只，剩余现金 {self.cash:.2f}元")

    # -------- 主循环 --------

    def run(self) -> dict:
        """执行完整回测，返回报告字典"""
        today = datetime.now().strftime("%Y%m%d")
        start_date = _nearest_trade_date_before(
            (datetime.now() - timedelta(weeks=self.sim_weeks)).strftime("%Y%m%d")
        )

        logger.info("=" * 70)
        logger.info(f"BBBIG 策略回测模拟器启动")
        logger.info(f"初始资金: {self.initial_capital:,.0f}元 | 回测周期: {self.sim_weeks}周")
        logger.info(f"起始日期: {start_date} | 截止日期: {today}")
        logger.info("=" * 70)

        # 获取回测区间所有交易日
        all_trade_dates = _get_trade_dates_from_db(start_date, today)
        if not all_trade_dates:
            logger.error("无法获取交易日历")
            return {}

        logger.info(f"共 {len(all_trade_dates)} 个交易日")

        # 初始净值快照
        self._nav_snapshot(start_date, "初始")

        # 按周分组
        current_risk = "中"
        week_idx = 0
        i = 0

        while i < len(all_trade_dates):
            selection_date = all_trade_dates[i]
            # 本周交易日范围（7个自然日内）
            sel_dt = datetime.strptime(selection_date, "%Y%m%d")
            week_end_dt = sel_dt + timedelta(days=7)
            week_end = week_end_dt.strftime("%Y%m%d")

            # Bug1修复：正确分组，移除错误的 or d == all_trade_dates[-1]
            week_dates = [d for d in all_trade_dates[i:] if d < week_end]
            if not week_dates:
                # 最后一周不足7天，取剩余全部日期
                week_dates = all_trade_dates[i:]
            if not week_dates:
                break

            week_idx += 1
            self._weekly_stop_loss_count = 0
            self._weekly_refill_blocked = False

            # 全局熔断检查：如果正在熔断中，递减剩余周数
            if self._global_circuit_breaker:
                if self._circuit_breaker_weeks_left > 0:
                    self._circuit_breaker_weeks_left -= 1
                if self._circuit_breaker_weeks_left <= 0:
                    self._global_circuit_breaker = False
                    self._global_stop_loss_streak = 0
                    logger.info(f"  第{week_idx}周 全局熔断解除，恢复正常交易")

            logger.info(f"\n{'='*60}")
            logger.info(f"第 {week_idx} 周 | 选股日: {selection_date} | "
                        f"周末: {week_dates[-1]}"
                        f"{' | ⚠️ 全局熔断中（仅做止损检查）' if self._global_circuit_breaker else ''}")
            logger.info(f"{'='*60}")
            logger.info(f"  周内风控: 中风险仓位45% | 止损后补仓冷静期"
                        f"{self.POST_STOP_LOSS_REFILL_COOLDOWN}个交易日 | "
                        f"同周止损满{self.WEEKLY_STOP_LOSS_REFILL_LIMIT}笔停补")

            # 1. 运行本周选股（全局熔断时跳过新建仓）
            if self._global_circuit_breaker:
                recs = []
                logger.info(f"  全局熔断中，跳过本周选股和新建仓，仅做持仓止损/止盈检查")
            else:
                recs, current_risk = self._run_weekly_selection(selection_date, current_risk)

            # 2. 计算本周可用建仓资金 = 总资产 × 仓位比例 - 现有持仓市值
            total_budget = self._total_assets() * self._position_ratio(current_risk)
            current_mv = sum(p.market_value for p in self.positions.values())
            available_for_buy = max(0.0, total_budget - current_mv)
            logger.info(f"  大盘风险: {current_risk}，仓位上限: "
                        f"{self._position_ratio(current_risk)*100:.0f}%，"
                        f"可建仓资金: {available_for_buy:,.0f}元")

            # 3. 次日（选股日后第一个交易日）尝试买入
            buy_date = _next_trade_date(selection_date)
            if buy_date <= today:
                # 优先处理上周末遗留的补仓买入
                if self._pending_refill_recs is not None:
                    self._process_pending_refill(buy_date)
                    total_budget = self._total_assets() * self._position_ratio(current_risk)
                    current_mv = sum(p.market_value for p in self.positions.values())
                    available_for_buy = max(0.0, total_budget - current_mv)

                self._buy_from_selections(recs, buy_date, available_for_buy)

            # 4. 本周逐日检查止损/目标价，卖出后触发补仓选股
            for j, trade_date in enumerate(week_dates):
                if trade_date <= buy_date:
                    continue  # 买入日当天不再检查（避免同日买卖）
                if trade_date > today:
                    break

                if self._pending_refill_recs is not None and not self._weekly_refill_blocked:
                    self._process_pending_refill(trade_date)

                exited, stop_loss_exited = self._check_exits(trade_date)
                if exited:
                    logger.info(f"  [{trade_date}] 触发卖出: {exited}")

                    if stop_loss_exited:
                        self._weekly_stop_loss_count += len(stop_loss_exited)
                        logger.info(f"  [{trade_date}] 本周累计止损 {self._weekly_stop_loss_count} 笔")
                        if self._weekly_stop_loss_count >= self.WEEKLY_STOP_LOSS_REFILL_LIMIT:
                            self._weekly_refill_blocked = True
                            if self._pending_refill_recs is not None:
                                logger.info(f"  [{trade_date}] 已取消未执行补仓，保留现金等待下周")
                                self._clear_pending_refill()
                            logger.info(f"  [{trade_date}] 本周累计止损达到"
                                        f"{self.WEEKLY_STOP_LOSS_REFILL_LIMIT}笔，停止本周剩余补仓")

                    # 卖出释放了仓位，触发补仓选股
                    refill_budget = self._total_assets() * self._position_ratio(current_risk)
                    refill_mv = sum(p.market_value for p in self.positions.values())
                    refill_available = max(0.0, refill_budget - refill_mv)
                    if refill_available < 1000:
                        continue
                    if self._global_circuit_breaker:
                        logger.info(f"  [{trade_date}] 仓位空缺 {refill_available:,.0f}元，"
                                    f"但全局熔断中，保留现金")
                        continue
                    if self._weekly_refill_blocked:
                        logger.info(f"  [{trade_date}] 仓位空缺 {refill_available:,.0f}元，"
                                    f"但已触发本周停补，保留现金")
                        continue

                    logger.info(f"  [{trade_date}] 仓位空缺 {refill_available:,.0f}元，"
                                f"触发补仓选股...")
                    refill_recs, current_risk = self._run_weekly_selection(
                        trade_date, current_risk)
                    refill_recs = [r for r in refill_recs
                                   if r.get("code", "") not in self.positions
                                   and r.get("code", "") not in exited]
                    if refill_recs:
                        cooldown_days = self.POST_STOP_LOSS_REFILL_COOLDOWN if stop_loss_exited else 0
                        refill_reason = (
                            f"止损后冷静期{self.POST_STOP_LOSS_REFILL_COOLDOWN}个交易日"
                            if stop_loss_exited else "基于前日卖出后选股"
                        )
                        self._schedule_refill(
                            refill_recs, refill_available, trade_date,
                            cooldown_days, refill_reason)
                    else:
                        logger.info(f"  [{trade_date}] 补仓选股无合适候选")

            # 跨周补仓：如果本周最后一天卖出产生的补仓选股结果
            # 保留在 self._pending_refill_recs 中，下周初自动处理

            # 5. 周末盯市净值快照
            week_last = week_dates[-1] if week_dates[-1] <= today else today
            self._mark_to_market(week_last)
            snap = self._nav_snapshot(week_last, f"第{week_idx}周末")
            logger.info(f"  周末净值: {snap['total_assets']:,.2f}元 "
                        f"({snap['return_pct']:+.2f}%) | "
                        f"持仓 {snap['position_count']} 只")

            # 移动到下一周
            next_start = week_end
            remaining = [d for d in all_trade_dates if d >= next_start]
            if not remaining:
                break
            i = all_trade_dates.index(remaining[0])

        # 5. 结束：将剩余持仓以最新价平仓（模拟强平）
        logger.info(f"\n{'='*60}")
        logger.info("回测结束，按最新价计算剩余持仓市值（不实际卖出）")
        self._mark_to_market(today)
        final_snap = self._nav_snapshot(today, "最终")
        logger.info(f"最终总资产: {final_snap['total_assets']:,.2f}元 "
                    f"({final_snap['return_pct']:+.2f}%)")

        return self._build_report(start_date, today)

    # -------- 报告 --------

    def _build_report(self, start_date: str, end_date: str) -> dict:
        total_assets = self._total_assets()
        total_return = (total_assets - self.initial_capital) / self.initial_capital * 100

        # 最大回撤
        nav_values = [s["total_assets"] for s in self.nav_history]
        max_drawdown = 0.0
        peak = nav_values[0] if nav_values else self.initial_capital
        for v in nav_values:
            if v > peak:
                peak = v
            dd = (peak - v) / peak * 100
            if dd > max_drawdown:
                max_drawdown = dd

        # 已平仓统计
        win_count  = sum(1 for p in self.closed_positions if p.realized_pnl > 0)
        loss_count = sum(1 for p in self.closed_positions if p.realized_pnl < 0)
        total_closed = len(self.closed_positions)
        win_rate = win_count / total_closed * 100 if total_closed > 0 else 0.0

        report = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "initial_capital": self.initial_capital,
            "start_date": start_date,
            "end_date": end_date,
            "sim_weeks": self.sim_weeks,
            "final_assets": round(total_assets, 2),
            "final_cash": round(self.cash, 2),
            "total_return_pct": round(total_return, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "win_count": win_count,
            "loss_count": loss_count,
            "total_closed_trades": total_closed,
            "win_rate": round(win_rate, 1),
            "nav_history": self.nav_history,
            "trade_log": self.trade_log,
            "closed_positions": [p.to_dict() for p in self.closed_positions],
            "open_positions": [p.to_dict() for p in self.positions.values()],
        }

        # 保存
        fpath = os.path.join(
            RESULT_DIR,
            f"simulation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        try:
            with open(fpath, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2, default=str)
            logger.info(f"回测报告已保存: {fpath}")
        except Exception as e:
            logger.error(f"保存报告失败: {e}")

        return report

    @staticmethod
    def format_report(report: dict) -> str:
        """格式化回测报告为可读文本"""
        if not report:
            return "无报告数据"
        lines = []
        lines.append("=" * 70)
        lines.append(f"  BBBIG 策略回测模拟报告  {report.get('timestamp', '')}")
        lines.append("=" * 70)
        lines.append(f"  初始资金:   {report['initial_capital']:>12,.2f} 元")
        lines.append(f"  最终资产:   {report['final_assets']:>12,.2f} 元")
        lines.append(f"  总收益率:   {report['total_return_pct']:>+11.2f} %")
        lines.append(f"  最大回撤:   {report['max_drawdown_pct']:>11.2f} %")
        lines.append(f"  回测区间:   {report['start_date']} → {report['end_date']}（{report['sim_weeks']}周）")
        lines.append(f"  已平仓笔数: {report['total_closed_trades']}  "
                     f"盈利: {report['win_count']}  亏损: {report['loss_count']}  "
                     f"胜率: {report['win_rate']:.1f}%")
        lines.append("")

        # 净值曲线
        lines.append("─" * 70)
        lines.append("  📈 净值历史")
        lines.append("─" * 70)
        for snap in report.get("nav_history", []):
            bar_len = max(0, int(snap["return_pct"] / 2))
            bar = "▓" * bar_len if snap["return_pct"] >= 0 else "░" * abs(bar_len)
            lines.append(f"  {snap['date']} [{snap['label']:<6}] "
                         f"总资产 {snap['total_assets']:>10,.2f}  "
                         f"{snap['return_pct']:>+7.2f}%  {bar}")

        # 交易记录
        lines.append("")
        lines.append("─" * 70)
        lines.append("  📋 交易记录")
        lines.append("─" * 70)
        for t in report.get("trade_log", []):
            if t["action"] == "BUY":
                lines.append(f"  🟢 买入 {t['date']} {t['code']} {t['name']:<8} "
                             f"@ {t['price']:.2f} × {t['shares']}股 = {t['cost']:.0f}元 "
                             f"(目标{t['target']:.2f} 止损{t['stop']:.2f})")
            else:
                pnl = t.get("pnl", 0)
                mark = "✅" if pnl > 0 else "❌"
                lines.append(f"  {mark} 卖出 {t['date']} {t['code']} {t['name']:<8} "
                             f"@ {t['price']:.2f} × {t['shares']}股 "
                             f"实收{t['proceeds']:.0f}元  盈亏{pnl:+.0f}元  [{t['status']}]")

        # 剩余持仓
        open_pos = report.get("open_positions", [])
        if open_pos:
            lines.append("")
            lines.append("─" * 70)
            lines.append("  📦 剩余持仓（按最新价）")
            lines.append("─" * 70)
            for p in open_pos:
                pnl = p.get("pnl", 0)
                pnl_type = p.get("pnl_type", "浮动")
                lines.append(f"  {p['code']} {p['name']:<8} 买入{p['buy_price']:.2f} "
                             f"× {p['shares']}股  目标{p['target_price']:.2f} "
                             f"止损{p['stop_loss']:.2f}  "
                             f"{pnl_type}盈亏 {pnl:+.0f}元")

        lines.append("")
        lines.append("  ⚠ 免责声明：回测结果仅供参考，历史表现不代表未来收益。投资有风险，入市需谨慎。")
        lines.append("=" * 70)
        return "\n".join(lines)


# ========== CLI 入口 ==========

def run_simulation(initial_capital: float = 30000.0, sim_weeks: int = 4,
                   quant_mode: bool = False) -> dict:
    sim = BBBIGSimulator(initial_capital=initial_capital, sim_weeks=sim_weeks,
                         quant_mode=quant_mode)
    report = sim.run()
    print(BBBIGSimulator.format_report(report))
    return report


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    capital = float(sys.argv[1]) if len(sys.argv) > 1 else 30000.0
    weeks   = int(sys.argv[2])   if len(sys.argv) > 2 else 4
    quant   = "--quant" in sys.argv or "-q" in sys.argv
    run_simulation(capital, weeks, quant_mode=quant)
