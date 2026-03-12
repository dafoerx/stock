#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测验证模块
模拟站在 N 周前的视角，使用相同的选股方法分析，
然后用真实后续K线数据验证买入区间、目标价、止损价的盈亏情况
"""
import logging
import time
import random
import json
import re
import numpy as np

class _JsonEncoder(json.JSONEncoder):
    """处理 numpy bool/int/float 的 JSON 序列化"""
    def default(self, obj):
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        return super().default(obj)
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from BBBIG.data_fetcher import fetcher
from BBBIG.deepseek_client import deepseek
from BBBIG.config import TOP_N, KLINE_DAYS, RESULT_DIR

import os

logger = logging.getLogger("BBBIG")


def _get_trade_date_before(weeks: int) -> str:
    """获取 N 周前的日期（回退到最近交易日），返回 YYYYMMDD"""
    target = datetime.now() - timedelta(weeks=weeks)
    # 如果是周末就回退到周五
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    return target.strftime("%Y%m%d")


def _fetch_kline_for_backtest(code: str, analysis_date: str, days_before: int = 30,
                               days_after: int = 10) -> tuple:
    """
    获取回测所需的K线数据
    :param code: 股票代码
    :param analysis_date: 分析基准日期 YYYYMMDD
    :param days_before: 分析日之前取多少天K线（给大模型看的）
    :param days_after: 分析日之后取多少天K线（用于验证）
    :return: (before_df, after_df)
    """
    ref_date = datetime.strptime(analysis_date, "%Y%m%d")
    # 获取分析日之前的K线
    before_df = fetcher.fetch_stock_kline(code, days=days_before, end_date_str=analysis_date)
    # 获取分析日之后的K线（用于验证）
    future_end = (ref_date + timedelta(days=days_after + 10)).strftime("%Y%m%d")
    today_str = datetime.now().strftime("%Y%m%d")
    if future_end > today_str:
        future_end = today_str
    full_df = fetcher.fetch_stock_kline(code, days=days_before + days_after + 20)
    if full_df.empty or before_df.empty:
        return before_df, pd.DataFrame()

    # 从完整K线中截取分析日之后的部分
    after_df = full_df[full_df["日期"] > ref_date.strftime("%Y-%m-%d")].head(days_after)
    return before_df, after_df


def _format_kline_for_prompt(kline_df: pd.DataFrame) -> str:
    """将K线格式化为文本供大模型分析"""
    if kline_df.empty:
        return "无数据"
    recent = kline_df.tail(7)
    lines = []
    for _, row in recent.iterrows():
        lines.append(
            f"{row['日期']}: 开{row['开盘']:.2f} 高{row['最高']:.2f} "
            f"低{row['最低']:.2f} 收{row['收盘']:.2f} "
            f"量{row['成交量']:.0f} 额{row['成交额']:.0f} "
            f"涨跌幅{row['涨跌幅']:.2f}% 换手{row['换手率']:.2f}%"
        )
    if len(kline_df) >= 5:
        week_start = kline_df.iloc[-5]["收盘"]
        week_end = kline_df.iloc[-1]["收盘"]
        week_change = (week_end - week_start) / week_start * 100
        lines.append(f"近一周涨跌幅: {week_change:.2f}%")
    if len(kline_df) >= 10:
        avg_vol_prev = kline_df.iloc[-10:-5]["成交量"].mean()
        avg_vol_recent = kline_df.iloc[-5:]["成交量"].mean()
        if avg_vol_prev > 0:
            vol_ratio = avg_vol_recent / avg_vol_prev
            lines.append(f"近一周成交量相比上周变化: {vol_ratio:.2f}倍")
    return "\n".join(lines)


def _parse_price(val) -> float:
    """解析价格值，支持字符串和数字"""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        nums = re.findall(r"[\d.]+", val)
        if nums:
            return float(nums[0])
    return 0.0


def _parse_buy_range(val) -> tuple:
    """解析买入区间，返回 (low, high)"""
    if isinstance(val, str):
        nums = re.findall(r"[\d.]+", val)
        if len(nums) >= 2:
            return float(nums[0]), float(nums[1])
        elif len(nums) == 1:
            p = float(nums[0])
            return p * 0.98, p * 1.02
    if isinstance(val, (int, float)):
        return float(val) * 0.98, float(val) * 1.02
    return 0, 0


def _evaluate_recommendation(rec: dict, after_df: pd.DataFrame, before_df: pd.DataFrame) -> dict:
    """
    根据实际后续K线验证单只推荐股的表现
    :param rec: 推荐数据 (含 buy_range, target_price, stop_loss)
    :param after_df: 推荐日之后的真实K线
    :param before_df: 推荐日之前的K线（取收盘价作为参考）
    :return: 验证结果字典
    """
    result = {
        "code": rec.get("code", ""),
        "name": rec.get("name", ""),
        "suggested_buy_range": rec.get("suggested_buy_range", ""),
        "target_price": rec.get("target_price", ""),
        "stop_loss": rec.get("stop_loss", ""),
    }

    buy_low, buy_high = _parse_buy_range(rec.get("suggested_buy_range", ""))
    target = _parse_price(rec.get("target_price", 0))
    stop_loss = _parse_price(rec.get("stop_loss", 0))

    result["buy_low"] = buy_low
    result["buy_high"] = buy_high
    result["target_price_val"] = target
    result["stop_loss_val"] = stop_loss

    # 取买入价为区间中值
    buy_price = (buy_low + buy_high) / 2 if (buy_low > 0 and buy_high > 0) else 0
    result["buy_price"] = buy_price

    if after_df.empty or buy_price <= 0:
        result["status"] = "无后续数据"
        result["max_price"] = 0
        result["min_price"] = 0
        result["end_price"] = 0
        result["max_profit_pct"] = 0
        result["max_loss_pct"] = 0
        result["final_profit_pct"] = 0
        result["hit_target"] = False
        result["hit_stop_loss"] = False
        result["outcome"] = "无法验证"
        return result

    # 分析后续K线
    max_price = after_df["最高"].max()
    min_price = after_df["最低"].min()
    end_price = after_df.iloc[-1]["收盘"]

    result["max_price"] = max_price
    result["min_price"] = min_price
    result["end_price"] = end_price
    result["max_profit_pct"] = round((max_price - buy_price) / buy_price * 100, 2)
    result["max_loss_pct"] = round((min_price - buy_price) / buy_price * 100, 2)
    result["final_profit_pct"] = round((end_price - buy_price) / buy_price * 100, 2)

    # 是否触达目标价
    result["hit_target"] = (max_price >= target) if target > 0 else False
    # 是否触达止损价
    result["hit_stop_loss"] = (min_price <= stop_loss) if stop_loss > 0 else False

    # 判断先触发哪个
    target_day = None
    stop_day = None
    for i, (_, row) in enumerate(after_df.iterrows()):
        if target > 0 and row["最高"] >= target and target_day is None:
            target_day = i
        if stop_loss > 0 and row["最低"] <= stop_loss and stop_day is None:
            stop_day = i

    if result["hit_target"] and result["hit_stop_loss"]:
        if target_day is not None and stop_day is not None:
            if target_day <= stop_day:
                result["outcome"] = "盈利（先触达目标价）"
                result["realized_pct"] = round((target - buy_price) / buy_price * 100, 2)
            else:
                result["outcome"] = "止损（先触达止损价）"
                result["realized_pct"] = round((stop_loss - buy_price) / buy_price * 100, 2)
        else:
            result["outcome"] = "盈利（触达目标价）"
            result["realized_pct"] = round((target - buy_price) / buy_price * 100, 2)
    elif result["hit_target"]:
        result["outcome"] = "盈利（触达目标价）"
        result["realized_pct"] = round((target - buy_price) / buy_price * 100, 2)
    elif result["hit_stop_loss"]:
        result["outcome"] = "止损"
        result["realized_pct"] = round((stop_loss - buy_price) / buy_price * 100, 2)
    else:
        if end_price >= buy_price:
            result["outcome"] = "浮盈（未触达目标/止损）"
        else:
            result["outcome"] = "浮亏（未触达目标/止损）"
        result["realized_pct"] = result["final_profit_pct"]

    # 后续K线概要
    kline_summary = []
    for _, row in after_df.iterrows():
        kline_summary.append(f"{row['日期']}: 收{row['收盘']:.2f} 高{row['最高']:.2f} 低{row['最低']:.2f}")
    result["after_kline_summary"] = "\n".join(kline_summary)

    return result


def _run_selection_at_date(analysis_date: str, candidate_codes: list = None) -> dict:
    """
    模拟站在指定日期进行选股分析
    :param analysis_date: 分析基准日 YYYYMMDD
    :param candidate_codes: 如指定则只分析这些股票，否则从全量中选
    :return: 类似 run_stock_selection 的结果 + 后续验证数据
    """
    ref_date = datetime.strptime(analysis_date, "%Y%m%d")
    ref_str = ref_date.strftime("%Y-%m-%d")
    logger.info(f"[回测] 模拟分析日期: {ref_str}")

    # 获取候选股的历史K线（分析日之前30天）
    # 由于实时行情无法回到历史，我们用K线数据末端的指标来替代
    # 先获取一批活跃股票
    if candidate_codes is None:
        logger.info("[回测] 获取当前活跃股列表作为候选池...")
        all_stocks = fetcher.fetch_all_stocks()
        if all_stocks.empty:
            return {"error": "获取A股行情失败"}
        # 用当前行情做粗筛（回测不完美但足够验证）
        from BBBIG.stock_selector import _prefilter_candidates
        candidates = _prefilter_candidates(all_stocks, top_n=60)
        candidate_codes = candidates["代码"].tolist()
        candidate_names = dict(zip(candidates["代码"], candidates["名称"]))
        candidate_industries = dict(zip(candidates["代码"], candidates["所处行业"]))
    else:
        candidate_names = {}
        candidate_industries = {}

    # 批量获取K线（分析日之前）
    logger.info(f"[回测] 获取 {len(candidate_codes)} 只股票历史K线...")
    kline_before_map = {}
    kline_after_map = {}

    def _fetch_one(code):
        time.sleep(random.uniform(0.1, 0.4))
        before_df, after_df = _fetch_kline_for_backtest(
            code, analysis_date, days_before=KLINE_DAYS, days_after=10
        )
        return code, before_df, after_df

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_one, c): c for c in candidate_codes}
        for future in as_completed(futures):
            try:
                code, before_df, after_df = future.result()
                if not before_df.empty:
                    kline_before_map[code] = before_df
                    kline_after_map[code] = after_df
            except Exception as e:
                logger.warning(f"[回测] 获取K线异常: {e}")

    logger.info(f"[回测] 成功获取 {len(kline_before_map)} 只股票K线")

    if not kline_before_map:
        return {"error": "无法获取历史K线数据"}

    # 构造 prompt 让大模型选股
    stock_summaries = []
    for code, before_df in kline_before_map.items():
        if before_df.empty:
            continue
        last_row = before_df.iloc[-1]
        kline_text = _format_kline_for_prompt(before_df)
        name = candidate_names.get(code, code)
        industry = candidate_industries.get(code, "-")
        stock_summaries.append(
            f"【{code} {name}】 行业:{industry} "
            f"最新价:{last_row['收盘']:.2f} 涨跌幅:{last_row['涨跌幅']:.2f}% "
            f"换手率:{last_row['换手率']:.2f}%\n"
            f"  K线数据:\n  {kline_text}"
        )

    system_prompt = """你是一位资深的A股量化分析师和投资顾问。请基于提供的市场数据进行专业分析。
你需要综合考虑以下因素：
1. 个股的K线形态（是否有上升趋势、支撑位是否稳固）
2. 成交量变化（是否放量、量价配合是否合理）
3. 技术面信号（均线趋势、MACD、KDJ等隐含信号）

你的分析必须客观、专业，给出明确的投资建议。"""

    user_prompt = f"""请分析以下A股候选股票数据（K线截止日期为 {ref_str}），
从中选出最值得投资的前{TOP_N}支股票。

以下是候选股票（含最近K线数据）：

{''.join(stock_summaries[:40])}

请完成以下分析任务：
1. 综合K线趋势、成交量变化等因素，选出最值得投资的前{TOP_N}支股票
2. 对每只推荐股票给出具体的买入区间、目标价、止损价

请严格按以下JSON格式返回：
```json
{{
  "market_analysis": "对当前市场趋势的分析（100字以内）",
  "recommendations": [
    {{
      "rank": 1,
      "code": "股票代码",
      "name": "股票名称",
      "current_price": 截止分析日的收盘价,
      "reason": "推荐理由（100字以内）",
      "suggested_buy_range": "建议买入区间（如 12.5-13.0）",
      "target_price": 目标价数字,
      "stop_loss": 止损价数字
    }}
  ]
}}
```"""

    logger.info("[回测] 调用 DeepSeek 分析选股...")
    analysis_result = deepseek.analyze_for_json(system_prompt, user_prompt, temperature=0.2, max_tokens=4096)

    recommendations = []
    if isinstance(analysis_result, dict) and "recommendations" in analysis_result:
        recommendations = analysis_result["recommendations"]

    # 验证每只推荐股
    logger.info(f"[回测] 大模型推荐 {len(recommendations)} 只股票，开始验证...")
    evaluations = []
    for rec in recommendations:
        code = rec.get("code", "")
        after_df = kline_after_map.get(code, pd.DataFrame())
        before_df = kline_before_map.get(code, pd.DataFrame())
        ev = _evaluate_recommendation(rec, after_df, before_df)
        evaluations.append(ev)
        status_mark = "✅" if "盈利" in ev.get("outcome", "") else (
            "❌" if "止损" in ev.get("outcome", "") else "➖")
        logger.info(f"  {status_mark} {code} {ev.get('name', '')}: {ev.get('outcome', '')} "
                    f"({ev.get('realized_pct', 0):+.2f}%)")

    return {
        "analysis_date": ref_str,
        "market_analysis": analysis_result.get("market_analysis", "") if isinstance(analysis_result, dict) else "",
        "recommendations": recommendations,
        "evaluations": evaluations,
    }


def run_backtest(weeks_list: list = None) -> dict:
    """
    执行完整回测：往前推 1/2/3 周，分别选股并验证
    :param weeks_list: 回测周数列表，默认 [1, 2, 3]
    :return: 完整回测报告
    """
    if weeks_list is None:
        weeks_list = [1, 2, 3]

    logger.info("=" * 70)
    logger.info("BBBIG 回测验证开始")
    logger.info(f"回测周数: {weeks_list}")
    logger.info("=" * 70)

    report = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "backtest_weeks": weeks_list,
        "rounds": [],
        "summary": {}
    }

    total_recs = 0
    total_profit = 0
    total_loss = 0
    total_hit_target = 0
    total_hit_stop = 0
    all_realized_pcts = []

    for weeks in weeks_list:
        analysis_date = _get_trade_date_before(weeks)
        logger.info(f"\n{'=' * 60}")
        logger.info(f"回测第 {weeks} 周前 (分析日: {analysis_date})")
        logger.info("=" * 60)

        round_result = _run_selection_at_date(analysis_date)

        if "error" in round_result:
            logger.error(f"回测第 {weeks} 周失败: {round_result['error']}")
            report["rounds"].append({
                "weeks_ago": weeks,
                "analysis_date": analysis_date,
                "error": round_result["error"]
            })
            continue

        evaluations = round_result.get("evaluations", [])
        round_profit = sum(1 for e in evaluations if "盈利" in e.get("outcome", ""))
        round_loss = sum(1 for e in evaluations if "止损" in e.get("outcome", ""))
        round_neutral = len(evaluations) - round_profit - round_loss

        round_summary = {
            "weeks_ago": weeks,
            "analysis_date": round_result["analysis_date"],
            "market_analysis": round_result.get("market_analysis", ""),
            "total_recommendations": len(evaluations),
            "profit_count": round_profit,
            "loss_count": round_loss,
            "neutral_count": round_neutral,
            "win_rate": f"{round_profit / len(evaluations) * 100:.1f}%" if evaluations else "0%",
            "evaluations": evaluations
        }

        # 计算平均收益
        pcts = [e.get("realized_pct", 0) for e in evaluations if "realized_pct" in e]
        if pcts:
            round_summary["avg_realized_pct"] = round(sum(pcts) / len(pcts), 2)
            all_realized_pcts.extend(pcts)
        else:
            round_summary["avg_realized_pct"] = 0

        report["rounds"].append(round_summary)

        total_recs += len(evaluations)
        total_profit += round_profit
        total_loss += round_loss
        total_hit_target += sum(1 for e in evaluations if e.get("hit_target"))
        total_hit_stop += sum(1 for e in evaluations if e.get("hit_stop_loss"))

    # 汇总
    report["summary"] = {
        "total_recommendations": total_recs,
        "total_profit": total_profit,
        "total_loss": total_loss,
        "total_neutral": total_recs - total_profit - total_loss,
        "overall_win_rate": f"{total_profit / total_recs * 100:.1f}%" if total_recs > 0 else "0%",
        "overall_loss_rate": f"{total_loss / total_recs * 100:.1f}%" if total_recs > 0 else "0%",
        "target_hit_rate": f"{total_hit_target / total_recs * 100:.1f}%" if total_recs > 0 else "0%",
        "stop_hit_rate": f"{total_hit_stop / total_recs * 100:.1f}%" if total_recs > 0 else "0%",
        "avg_realized_pct": round(sum(all_realized_pcts) / len(all_realized_pcts), 2) if all_realized_pcts else 0,
    }

    # 保存结果
    fpath = os.path.join(RESULT_DIR, f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    try:
        with open(fpath, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, cls=_JsonEncoder)
        logger.info(f"回测结果已保存至: {fpath}")
    except Exception as e:
        logger.error(f"保存回测结果异常: {e}")

    return report


def format_backtest_report(report: dict) -> str:
    """将回测结果格式化为可读报告"""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  BBBIG 选股回测验证报告  {report['timestamp']}")
    lines.append("=" * 70)

    for rd in report.get("rounds", []):
        if "error" in rd:
            lines.append(f"\n[{rd['weeks_ago']}周前] 分析日 {rd['analysis_date']} — 错误: {rd['error']}")
            continue

        lines.append(f"\n{'─' * 70}")
        lines.append(f"  📅 回测: {rd['weeks_ago']}周前 | 分析日: {rd['analysis_date']}")
        lines.append(f"  推荐 {rd['total_recommendations']} 只 | "
                     f"盈利 {rd['profit_count']} | 止损 {rd['loss_count']} | "
                     f"持平 {rd['neutral_count']} | 胜率 {rd['win_rate']} | "
                     f"平均收益 {rd['avg_realized_pct']:+.2f}%")
        lines.append(f"{'─' * 70}")

        if rd.get("market_analysis"):
            lines.append(f"  市场分析: {rd['market_analysis']}")
            lines.append("")

        for ev in rd.get("evaluations", []):
            outcome = ev.get("outcome", "")
            if "盈利" in outcome:
                mark = "✅"
            elif "止损" in outcome:
                mark = "❌"
            elif "浮盈" in outcome:
                mark = "📈"
            else:
                mark = "📉"

            lines.append(f"  {mark} {ev['code']} {ev['name']}")
            lines.append(f"     买入区间: {ev['suggested_buy_range']}  |  买入价(中值): {ev['buy_price']:.2f}")
            lines.append(f"     目标价: {ev['target_price']}  |  止损价: {ev['stop_loss']}")
            lines.append(f"     后续最高: {ev['max_price']:.2f} ({ev['max_profit_pct']:+.2f}%)  |  "
                         f"后续最低: {ev['min_price']:.2f} ({ev['max_loss_pct']:+.2f}%)")
            lines.append(f"     期末收盘: {ev['end_price']:.2f} ({ev['final_profit_pct']:+.2f}%)")
            lines.append(f"     结果: {outcome}  |  实际收益: {ev.get('realized_pct', 0):+.2f}%")
            if ev.get("after_kline_summary"):
                lines.append(f"     后续K线:")
                for kl in ev["after_kline_summary"].split("\n"):
                    lines.append(f"       {kl}")
            lines.append("")

    # 汇总
    summary = report.get("summary", {})
    lines.append("=" * 70)
    lines.append("  📊 回测汇总")
    lines.append("=" * 70)
    lines.append(f"  总推荐数: {summary.get('total_recommendations', 0)}")
    lines.append(f"  盈利数: {summary.get('total_profit', 0)}  |  止损数: {summary.get('total_loss', 0)}  |  "
                 f"未触发: {summary.get('total_neutral', 0)}")
    lines.append(f"  总胜率: {summary.get('overall_win_rate', '0%')}")
    lines.append(f"  总止损率: {summary.get('overall_loss_rate', '0%')}")
    lines.append(f"  目标价触达率: {summary.get('target_hit_rate', '0%')}")
    lines.append(f"  止损价触达率: {summary.get('stop_hit_rate', '0%')}")
    lines.append(f"  平均实际收益: {summary.get('avg_realized_pct', 0):+.2f}%")
    lines.append("")
    lines.append("  ⚠ 免责声明：回测结果仅供参考，历史表现不代表未来收益。")
    lines.append("=" * 70)
    return "\n".join(lines)


def backtest_stock_list(stock_list: list, weeks_list: list = None) -> dict:
    """
    对指定的股票列表进行多周回测验证，返回每只股票的盈利概率
    :param stock_list: 推荐股票列表，每项含 code, name, suggested_buy_range, target_price, stop_loss
    :param weeks_list: 回测周数列表，默认 [1, 2, 3]
    :return: {
        "stock_results": [{"code", "name", "win_count", "loss_count", "total_rounds",
                           "win_rate", "avg_profit_pct", "rounds": [...]}],
        "summary": {...}
    }
    """
    if weeks_list is None:
        weeks_list = [1, 2, 3]

    logger.info("=" * 70)
    logger.info("BBBIG 推荐股票回测验证")
    logger.info(f"回测 {len(stock_list)} 只推荐股票，往前推 {weeks_list} 周")
    logger.info("=" * 70)

    # 为每只股票在每个时间窗口做回测
    stock_results = []
    for rec in stock_list:
        code = rec.get("code", "")
        name = rec.get("name", code)
        if not code:
            continue

        logger.info(f"\n[回测] {code} {name} ...")
        rounds = []
        win_count = 0
        loss_count = 0
        neutral_count = 0
        all_pcts = []

        for weeks in weeks_list:
            analysis_date = _get_trade_date_before(weeks)
            ref_date = datetime.strptime(analysis_date, "%Y%m%d")
            ref_str = ref_date.strftime("%Y-%m-%d")

            try:
                time.sleep(random.uniform(0.2, 0.5))
                before_df, after_df = _fetch_kline_for_backtest(
                    code, analysis_date, days_before=KLINE_DAYS, days_after=10
                )
            except Exception as e:
                logger.warning(f"  获取 {code} K线异常: {e}")
                rounds.append({"weeks_ago": weeks, "date": ref_str, "outcome": "数据异常"})
                continue

            if before_df.empty:
                rounds.append({"weeks_ago": weeks, "date": ref_str, "outcome": "无历史数据"})
                continue

            # 用历史K线让大模型重新给出买入区间/目标价/止损价
            kline_text = _format_kline_for_prompt(before_df)
            last_close = before_df.iloc[-1]["收盘"]

            system_prompt = """你是一位资深的A股量化分析师。请基于提供的K线数据给出精确的交易建议。"""

            user_prompt = f"""请分析以下股票的K线数据（截止 {ref_str}），给出交易建议。

【{code} {name}】 当前价: {last_close:.2f}
K线数据:
{kline_text}

请严格按以下JSON格式返回：
```json
{{
  "code": "{code}",
  "name": "{name}",
  "current_price": {last_close:.2f},
  "suggested_buy_range": "建议买入区间（如 12.5-13.0）",
  "target_price": 目标价数字,
  "stop_loss": 止损价数字,
  "reason": "分析理由（50字以内）"
}}
```"""

            logger.info(f"  [{weeks}周前 {ref_str}] 调用 DeepSeek 分析...")
            analysis_result = deepseek.analyze_for_json(system_prompt, user_prompt, temperature=0.2, max_tokens=1024)

            if not isinstance(analysis_result, dict) or "suggested_buy_range" not in analysis_result:
                logger.warning(f"  大模型返回格式异常，使用原始推荐数据")
                analysis_result = {
                    "code": code,
                    "name": name,
                    "suggested_buy_range": rec.get("suggested_buy_range", ""),
                    "target_price": rec.get("target_price", 0),
                    "stop_loss": rec.get("stop_loss", 0),
                }

            # 验证
            ev = _evaluate_recommendation(analysis_result, after_df, before_df)
            outcome = ev.get("outcome", "")

            round_info = {
                "weeks_ago": weeks,
                "date": ref_str,
                "buy_range": ev.get("suggested_buy_range", ""),
                "buy_price": ev.get("buy_price", 0),
                "target_price": ev.get("target_price", ""),
                "stop_loss": ev.get("stop_loss", ""),
                "max_price": ev.get("max_price", 0),
                "min_price": ev.get("min_price", 0),
                "end_price": ev.get("end_price", 0),
                "outcome": outcome,
                "realized_pct": ev.get("realized_pct", 0),
            }
            rounds.append(round_info)

            if "盈利" in outcome:
                win_count += 1
            elif "止损" in outcome:
                loss_count += 1
            else:
                neutral_count += 1

            if "realized_pct" in ev:
                all_pcts.append(ev["realized_pct"])

            status_mark = "✅" if "盈利" in outcome else ("❌" if "止损" in outcome else "➖")
            logger.info(f"  {status_mark} [{weeks}周前] {outcome} ({ev.get('realized_pct', 0):+.2f}%)")

        total_rounds = win_count + loss_count + neutral_count
        win_rate = (win_count / total_rounds * 100) if total_rounds > 0 else 0
        avg_pct = (sum(all_pcts) / len(all_pcts)) if all_pcts else 0

        stock_results.append({
            "code": code,
            "name": name,
            "industry": rec.get("industry", ""),
            "current_price": rec.get("current_price", 0),
            "reason": rec.get("reason", ""),
            "suggested_buy_range": rec.get("suggested_buy_range", ""),
            "target_price": rec.get("target_price", ""),
            "stop_loss": rec.get("stop_loss", ""),
            "win_count": win_count,
            "loss_count": loss_count,
            "neutral_count": neutral_count,
            "total_rounds": total_rounds,
            "win_rate": round(win_rate, 1),
            "avg_profit_pct": round(avg_pct, 2),
            "rounds": rounds,
        })

    # 按盈利概率降序排序
    stock_results.sort(key=lambda x: (x["win_rate"], x["avg_profit_pct"]), reverse=True)

    # 重新编排 rank
    for i, sr in enumerate(stock_results):
        sr["rank"] = i + 1

    total_all = sum(sr["total_rounds"] for sr in stock_results)
    total_wins = sum(sr["win_count"] for sr in stock_results)
    total_losses = sum(sr["loss_count"] for sr in stock_results)
    all_avgs = [sr["avg_profit_pct"] for sr in stock_results if sr["total_rounds"] > 0]

    report = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "backtest_weeks": weeks_list,
        "stock_results": stock_results,
        "summary": {
            "total_stocks": len(stock_results),
            "total_rounds": total_all,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "overall_win_rate": f"{total_wins / total_all * 100:.1f}%" if total_all > 0 else "0%",
            "avg_profit_pct": round(sum(all_avgs) / len(all_avgs), 2) if all_avgs else 0,
        }
    }

    # 保存结果
    fpath = os.path.join(RESULT_DIR, f"backtest_ranked_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    try:
        with open(fpath, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, cls=_JsonEncoder)
        logger.info(f"回测排序结果已保存至: {fpath}")
    except Exception as e:
        logger.error(f"保存回测排序结果异常: {e}")

    return report


def format_ranked_backtest_report(report: dict) -> str:
    """将按盈利概率排序的回测结果格式化为可读报告"""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  BBBIG 选股推荐 × 回测验证报告  {report['timestamp']}")
    lines.append(f"  回测方式: 往前推 {report['backtest_weeks']} 周，用相同方法分析后验证盈亏")
    lines.append("=" * 70)

    lines.append(f"\n  {'排名':<4} {'代码':<8} {'名称':<8} {'盈利概率':>8} "
                 f"{'盈/亏/平':>8} {'平均收益':>8} {'买入区间':>14} {'目标价':>8} {'止损价':>8}")
    lines.append("─" * 70)

    for sr in report.get("stock_results", []):
        wl_str = f"{sr['win_count']}/{sr['loss_count']}/{sr['neutral_count']}"
        lines.append(
            f"  #{sr['rank']:<3} {sr['code']:<8} {sr['name']:<8} {sr['win_rate']:>6.1f}% "
            f"{wl_str:>8} {sr['avg_profit_pct']:>+7.2f}% "
            f"{str(sr.get('suggested_buy_range', '')):>14} "
            f"{str(sr.get('target_price', '')):>8} "
            f"{str(sr.get('stop_loss', '')):>8}"
        )

    lines.append("─" * 70)

    # 详细每只股票的回测明细
    lines.append(f"\n{'=' * 70}")
    lines.append("  各股票回测明细")
    lines.append("=" * 70)

    for sr in report.get("stock_results", []):
        lines.append(f"\n  #{sr['rank']} {sr['code']} {sr['name']} "
                     f"— 盈利概率 {sr['win_rate']:.1f}% | 平均收益 {sr['avg_profit_pct']:+.2f}%")
        if sr.get("reason"):
            lines.append(f"  推荐理由: {sr['reason']}")
        lines.append(f"  买入区间: {sr.get('suggested_buy_range', '-')} | "
                     f"目标价: {sr.get('target_price', '-')} | 止损价: {sr.get('stop_loss', '-')}")

        for rd in sr.get("rounds", []):
            outcome = rd.get("outcome", "")
            if "盈利" in outcome:
                mark = "✅"
            elif "止损" in outcome:
                mark = "❌"
            else:
                mark = "➖"
            lines.append(f"    {mark} {rd['weeks_ago']}周前({rd['date']}): "
                         f"买{rd.get('buy_price', 0):.2f} → "
                         f"高{rd.get('max_price', 0):.2f} 低{rd.get('min_price', 0):.2f} "
                         f"收{rd.get('end_price', 0):.2f} | "
                         f"{outcome} ({rd.get('realized_pct', 0):+.2f}%)")
        lines.append("")

    # 汇总
    summary = report.get("summary", {})
    lines.append("=" * 70)
    lines.append("  📊 汇总")
    lines.append("=" * 70)
    lines.append(f"  总股票数: {summary.get('total_stocks', 0)} | "
                 f"总回测轮数: {summary.get('total_rounds', 0)}")
    lines.append(f"  总盈利: {summary.get('total_wins', 0)} | "
                 f"总止损: {summary.get('total_losses', 0)} | "
                 f"总胜率: {summary.get('overall_win_rate', '0%')}")
    lines.append(f"  平均收益: {summary.get('avg_profit_pct', 0):+.2f}%")
    lines.append("")
    lines.append("  ⚠ 免责声明：回测结果仅供参考，历史表现不代表未来收益。投资有风险，入市需谨慎。")
    lines.append("=" * 70)
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    report = run_backtest([1, 2, 3])
    print(format_backtest_report(report))
