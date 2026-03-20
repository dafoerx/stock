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
from typing import Optional

import numpy as np
import pandas as pd

from BBBIG.data_fetcher import fetcher
from BBBIG.config import DB_FILE, RESULT_DIR, KLINE_DAYS

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
    返回的 DataFrame 含列: 日期(str YYYY-MM-DD), 开盘, 最高, 最低, 收盘, 成交量
    日期升序排列。
    """
    try:
        conn = sqlite3.connect(DB_FILE)
        # 将 YYYYMMDD 转为 YYYY-MM-DD 用于 daily 表查询
        sd = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        ed = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
        df = pd.read_sql_query(
            "SELECT trade_date, open, high, low, close, vol "
            "FROM daily WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ? "
            "ORDER BY trade_date ASC",
            conn,
            params=(code if '.' in code else code + '.SZ', sd, ed)
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

    def __init__(self, initial_capital: float = 30000.0, sim_weeks: int = 4):
        """
        :param initial_capital: 初始资金（元）
        :param sim_weeks: 回测总周数（从多少周前开始）
        """
        self.initial_capital = initial_capital
        self.sim_weeks = sim_weeks

        self.cash = initial_capital          # 当前可用资金
        self.positions: dict[str, Position] = {}   # code -> Position（持有中）
        self.closed_positions: list[Position] = []  # 已平仓
        self.trade_log: list[dict] = []
        self.nav_history: list[dict] = []          # 每周净值快照

        # 大盘风险 → 总仓位上限
        self._risk_to_position = {"低": 0.80, "中": 0.60, "高": 0.30}

        # Bug2修复：止损冷静期记录 code -> 止损日期
        self._stop_loss_dates: dict[str, str] = {}
        self.COOLDOWN_DAYS = 10  # 止损后N个交易日内不再买入同一只

        # 跨周补仓：上周末卖出后的待补仓选股结果
        self._pending_refill_recs: Optional[list] = None
        self._pending_refill_budget: float = 0.0

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
        return self._risk_to_position.get(risk_level, 0.60)

    # -------- 每日盯市 + 卖出检查 --------

    def _mark_to_market(self, date_str: str):
        """更新当日收盘价（盯市）"""
        for code, pos in list(self.positions.items()):
            kline = _get_kline_for_date_range(code, date_str, date_str)
            if not kline.empty:
                pos.current_price = float(kline.iloc[-1]["收盘"])

    def _check_exits(self, date_str: str) -> list:
        """
        检查当日是否触达目标价或止损价，按"先到先得"逻辑处理。
        返回已平仓的 code 列表。
        """
        exited = []
        for code, pos in list(self.positions.items()):
            kline = _get_kline_for_date_range(code, date_str, date_str)
            if kline.empty:
                continue
            row = kline.iloc[0]
            high = float(row["最高"])
            low  = float(row["最低"])
            open_price = float(row["开盘"])

            hit_target = (pos.target_price > 0) and (high >= pos.target_price)
            hit_stop   = (pos.stop_loss  > 0) and (low  <= pos.stop_loss)

            if not hit_target and not hit_stop:
                # 盯市更新
                pos.current_price = float(row["收盘"])
                continue

            # 判断先后：用开盘价判断当日跳空
            if hit_target and hit_stop:
                # 开盘跳空到目标价以上 → 先触目标
                if open_price >= pos.target_price:
                    sell_price = pos.target_price
                    pos.status = "盈利卖出"
                # 开盘跳空跌破止损 → 先触止损
                elif open_price <= pos.stop_loss:
                    sell_price = pos.stop_loss
                    pos.status = "止损卖出"
                else:
                    # 开盘在区间内：高点先到 or 低点先到？
                    # 通常用"涨跌幅"判断当日走势方向
                    close = float(row["收盘"])
                    if close >= open_price:
                        sell_price = pos.target_price
                        pos.status = "盈利卖出"
                    else:
                        sell_price = pos.stop_loss
                        pos.status = "止损卖出"
            elif hit_target:
                sell_price = pos.target_price
                pos.status = "盈利卖出"
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

            # Bug2修复：记录止损冷静期
            if pos.status == "止损卖出":
                self._stop_loss_dates[code] = date_str
                logger.info(f"  [{date_str}] {code} 进入止损冷静期（{self.COOLDOWN_DAYS}个交易日）")

        return exited

    # -------- 买入逻辑 --------

    def _try_buy(self, rec: dict, buy_date: str, available_cash: float) -> bool:
        """
        尝试在 buy_date 以推荐买入区间成交。
        当日实际价格（开盘~收盘范围）与区间有交集才成交。
        成交价取 min(buy_high, 当日最高价) 与 max(buy_low, 当日最低价) 的中值。
        :return: True=成交, False=未成交
        """
        code = rec.get("code", "")
        name = rec.get("name", code)
        if code in self.positions:
            return False  # 已持有

        # Bug2修复：检查止损冷静期
        if code in self._stop_loss_dates:
            sl_date = self._stop_loss_dates[code]
            # 获取止损日之后的交易日数
            all_dates_after = _get_trade_dates_from_db(sl_date, buy_date)
            days_since = len(all_dates_after) - 1  # 不含止损日本身
            if days_since < self.COOLDOWN_DAYS:
                logger.info(f"  [{buy_date}] {code} {name} 止损冷静期中（已{days_since}天，需{self.COOLDOWN_DAYS}天），跳过")
                return False

        buy_low, buy_high = _parse_buy_range(rec.get("suggested_buy_range", ""))
        if buy_low <= 0 or buy_high <= 0:
            logger.debug(f"  {code} 买入区间无效，跳过")
            return False

        target = _parse_price(rec.get("target_price", 0))
        stop   = _parse_price(rec.get("stop_loss", 0))

        kline = _get_kline_for_date_range(code, buy_date, buy_date)
        if kline.empty:
            logger.debug(f"  {code} {buy_date} 无K线数据，跳过")
            return False

        row = kline.iloc[0]
        day_low  = float(row["最低"])
        day_high = float(row["最高"])

        # 判断价格区间是否与当日K线有交集
        if day_high < buy_low or day_low > buy_high:
            logger.info(f"  [{buy_date}] {code} {name} 价格 [{day_low:.2f},{day_high:.2f}] "
                        f"未落入买入区间 [{buy_low:.2f},{buy_high:.2f}]，跳过")
            return False

        # 成交价：区间与当日K线的交集中点
        actual_low  = max(buy_low,  day_low)
        actual_high = min(buy_high, day_high)
        exec_price  = round((actual_low + actual_high) / 2, 3)

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
        alloc_cash = available_cash * rec_position_pct
        if alloc_cash < exec_price * 100:  # 至少买1手(100股)
            alloc_cash = min(available_cash, exec_price * 200)

        shares = int(alloc_cash / exec_price / 100) * 100  # 取整到100股
        if shares <= 0:
            logger.debug(f"  {code} 资金不足，无法买入")
            return False

        cost = _calc_buy_cost(exec_price, shares)
        if cost > self.cash:
            shares = int(self.cash / exec_price / 1.003 / 100) * 100
            if shares <= 0:
                return False
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
                    f"= {cost:.2f}元 | 目标{target:.2f} 止损{stop:.2f}")
        return True

    # -------- 每周选股 + 建仓 --------

    def _run_weekly_selection(self, selection_date: str, risk_level: str) -> list:
        """
        在 selection_date 重新运行完整选股（调用 stock_selector.run_stock_selection）。
        通过设置环境变量 BBBIG_BACKTEST_DATE 让 fetcher 的缓存查询以该日期为基准。
        返回推荐股列表。
        """
        from BBBIG.stock_selector import run_stock_selection
        logger.info(f"  [选股] 模拟 {selection_date} 的选股流程（真实AI选股）...")
        try:
            result = run_stock_selection()
            recs = result.get("recommendations", [])
            # 如果大盘风险更新了，取最新的
            market_risk = result.get("market_risk", {})
            if market_risk:
                risk_level = market_risk.get("risk_level", risk_level)
            logger.info(f"  [选股] 共选出 {len(recs)} 只股票，大盘风险={risk_level}")
            return recs, risk_level
        except Exception as e:
            logger.error(f"  [选股] 选股失败: {e}")
            return [], risk_level

    def _buy_from_selections(self, recs: list, buy_date: str, total_budget: float):
        """
        遍历推荐列表，尝试按仓位建仓。
        若某只未成交（价格未入区间），自动顺延尝试下一只备选。
        """
        if not recs:
            return

        bought = 0
        for rec in recs:
            code = rec.get("code", "")
            if code in self.positions:
                continue  # 已持有，跳过
            if self.cash < 1000:
                logger.info(f"  [{buy_date}] 可用资金不足1000元，停止建仓")
                break
            ok = self._try_buy(rec, buy_date, total_budget)
            if ok:
                bought += 1

        logger.info(f"  [{buy_date}] 本周建仓 {bought} 只，持仓 {len(self.positions)} 只，"
                    f"剩余现金 {self.cash:.2f}元")

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
            logger.info(f"\n{'='*60}")
            logger.info(f"第 {week_idx} 周 | 选股日: {selection_date} | "
                        f"周末: {week_dates[-1]}")
            logger.info(f"{'='*60}")

            # 1. 运行本周选股
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
                    logger.info(f"  [{buy_date}] 执行上周遗留补仓买入")
                    self._buy_from_selections(
                        self._pending_refill_recs, buy_date,
                        self._pending_refill_budget)
                    self._pending_refill_recs = None
                    self._pending_refill_budget = 0.0
                    # 重新计算可建仓资金
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

                # 如果前一日触发卖出产生了补仓选股结果，今天尝试买入
                if self._pending_refill_recs is not None:
                    logger.info(f"  [{trade_date}] 补仓买入（基于前日卖出后选股）")
                    self._buy_from_selections(
                        self._pending_refill_recs, trade_date,
                        self._pending_refill_budget)
                    self._pending_refill_recs = None
                    self._pending_refill_budget = 0.0

                exited = self._check_exits(trade_date)
                if exited:
                    logger.info(f"  [{trade_date}] 触发卖出: {exited}")
                    # 卖出释放了仓位，触发补仓选股
                    refill_budget = self._total_assets() * self._position_ratio(current_risk)
                    refill_mv = sum(p.market_value for p in self.positions.values())
                    refill_available = max(0.0, refill_budget - refill_mv)
                    if refill_available >= 1000:
                        logger.info(f"  [{trade_date}] 仓位空缺 {refill_available:,.0f}元，"
                                    f"触发补仓选股...")
                        refill_recs, current_risk = self._run_weekly_selection(
                            trade_date, current_risk)
                        # 过滤掉已持有和刚卖出的股票
                        refill_recs = [r for r in refill_recs
                                       if r.get("code", "") not in self.positions
                                       and r.get("code", "") not in exited]
                        if refill_recs:
                            self._pending_refill_recs = refill_recs
                            self._pending_refill_budget = refill_available
                            logger.info(f"  [{trade_date}] 补仓候选 {len(refill_recs)} 只，"
                                        f"次日买入")
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

def run_simulation(initial_capital: float = 30000.0, sim_weeks: int = 4) -> dict:
    sim = BBBIGSimulator(initial_capital=initial_capital, sim_weeks=sim_weeks)
    report = sim.run()
    print(BBBIGSimulator.format_report(report))
    return report


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    capital = float(sys.argv[1]) if len(sys.argv) > 1 else 30000.0
    weeks   = int(sys.argv[2])   if len(sys.argv) > 2 else 4
    run_simulation(capital, weeks)
