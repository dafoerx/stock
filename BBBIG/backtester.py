#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测验证模块（优化版）
- 模式A: AI回测 — 站在 N 周前视角，使用 DeepSeek 分析，用真实K线验证
- 模式B: 推荐股回测 — 对已推荐股票验证盈亏
- 模式C: 纯量化回测 — 不调AI，直接用多因子评分选股并验证（快速迭代因子）
"""
import logging
import time
import random
import json
import re
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from BBBIG.data_fetcher import fetcher
from BBBIG.deepseek_client import deepseek
from BBBIG.config import TOP_N, KLINE_DAYS, RESULT_DIR, AI_TEMPERATURE, AI_MAX_TOKENS, TOTAL_TRADE_COST

import os

logger = logging.getLogger("BBBIG")


def _get_trade_date_before(weeks: int) -> str:
    """获取 N 周前的最近交易日，优先用交易日历，回退到weekday判断"""
    target = datetime.now() - timedelta(weeks=weeks)
    target_str = target.strftime("%Y%m%d")

    # 优先使用交易日历
    try:
        from BBBIG.db_cache import db_cache
        # 查找目标日期前后5天范围内的最近交易日
        start = (target - timedelta(days=10)).strftime("%Y%m%d")
        dates = db_cache.get_trade_dates(start, target_str)
        if dates:
            return dates[-1]  # 取最近的交易日
    except Exception:
        pass

    # 回退: 跳过周末
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    return target.strftime("%Y%m%d")


def _fetch_kline_for_backtest(code: str, analysis_date: str, days_before: int = 30,
                               days_after: int = 10) -> tuple:
    """
    获取回测所需的K线数据
    :return: (before_df, after_df)
    """
    ref_date = datetime.strptime(analysis_date, "%Y%m%d")
    before_df = fetcher.fetch_stock_kline(code, days=days_before, end_date_str=analysis_date)
    future_end = (ref_date + timedelta(days=days_after + 10)).strftime("%Y%m%d")
    today_str = datetime.now().strftime("%Y%m%d")
    if future_end > today_str:
        future_end = today_str
    full_df = fetcher.fetch_stock_kline(code, days=days_before + days_after + 20)
    if full_df.empty or before_df.empty:
        return before_df, pd.DataFrame()
    after_df = full_df[full_df["日期"] > ref_date.strftime("%Y-%m-%d")].head(days_after)
    return before_df, after_df


def _format_kline_for_prompt(kline_df: pd.DataFrame) -> str:
    """将K线格式化为文本供大模型分析（增强版含技术指标）"""
    if kline_df.empty:
        return "无数据"

    from BBBIG.stock_selector import calc_technical_indicators

    recent = kline_df.tail(7)
    lines = []
    for _, row in recent.iterrows():
        lines.append(
            f"{row['日期']}: 开{row['开盘']:.2f} 高{row['最高']:.2f} "
            f"低{row['最低']:.2f} 收{row['收盘']:.2f} "
            f"量{row['成交量']:.0f} 额{row['成交额']:.0f} "
            f"涨跌幅{row['涨跌幅']:.2f}% 换手{row['换手率']:.2f}%"
        )

    # 技术指标
    indicators = calc_technical_indicators(kline_df)
    if indicators:
        lines.append("--- 技术指标 ---")
        lines.append(f"MA5:{indicators.get('ma5',0):.2f} MA10:{indicators.get('ma10',0):.2f} "
                     f"MA20:{indicators.get('ma20',0):.2f} {'多头' if indicators.get('ma_bull') else '空头' if indicators.get('ma_bear') else '交叉'}")
        lines.append(f"MACD:{indicators.get('macd_signal','未知')} RSI:{indicators.get('rsi',50):.1f} "
                     f"KDJ:{indicators.get('kdj_signal','未知')}")
        lines.append(f"近5日涨幅:{indicators.get('chg_5d',0):.2f}% 量能趋势:{indicators.get('vol_trend',1):.2f}倍")

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
            lines.append(f"成交量周环比: {vol_ratio:.2f}倍")
    return "\n".join(lines)


def _parse_price(val) -> float:
    """解析价格值"""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        nums = re.findall(r"[\d.]+", val)
        if nums:
            return float(nums[0])
    return 0.0


def _parse_buy_range(val) -> tuple:
    """解析买入区间"""
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
    """根据实际后续K线验证单只推荐股的表现"""
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

    buy_price = (buy_low + buy_high) / 2 if (buy_low > 0 and buy_high > 0) else 0
    result["buy_price"] = buy_price

    if after_df.empty or buy_price <= 0:
        result.update({
            "status": "无后续数据", "max_price": 0, "min_price": 0, "end_price": 0,
            "max_profit_pct": 0, "max_loss_pct": 0, "final_profit_pct": 0,
            "hit_target": False, "hit_stop_loss": False, "outcome": "无法验证",
            "realized_pct": 0
        })
        return result

    max_price = after_df["最高"].max()
    min_price = after_df["最低"].min()
    end_price = after_df.iloc[-1]["收盘"]

    result["max_price"] = max_price
    result["min_price"] = min_price
    result["end_price"] = end_price
    # 扣除交易成本（买卖佣金+印花税，约0.16%）
    cost_pct = TOTAL_TRADE_COST * 100  # 转为百分比
    result["max_profit_pct"] = round((max_price - buy_price) / buy_price * 100 - cost_pct, 2)
    result["max_loss_pct"] = round((min_price - buy_price) / buy_price * 100 - cost_pct, 2)
    result["final_profit_pct"] = round((end_price - buy_price) / buy_price * 100 - cost_pct, 2)

    result["hit_target"] = (max_price >= target) if target > 0 else False
    result["hit_stop_loss"] = (min_price <= stop_loss) if stop_loss > 0 else False

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
                result["realized_pct"] = round((target - buy_price) / buy_price * 100 - cost_pct, 2)
            else:
                result["outcome"] = "止损（先触达止损价）"
                result["realized_pct"] = round((stop_loss - buy_price) / buy_price * 100 - cost_pct, 2)
        else:
            result["outcome"] = "盈利（触达目标价）"
            result["realized_pct"] = round((target - buy_price) / buy_price * 100 - cost_pct, 2)
    elif result["hit_target"]:
        result["outcome"] = "盈利（触达目标价）"
        result["realized_pct"] = round((target - buy_price) / buy_price * 100 - cost_pct, 2)
    elif result["hit_stop_loss"]:
        result["outcome"] = "止损"
        result["realized_pct"] = round((stop_loss - buy_price) / buy_price * 100 - cost_pct, 2)
    else:
        if end_price >= buy_price:
            result["outcome"] = "浮盈（未触达目标/止损）"
        else:
            result["outcome"] = "浮亏（未触达目标/止损）"
        result["realized_pct"] = result["final_profit_pct"]

    kline_summary = []
    for _, row in after_df.iterrows():
        kline_summary.append(f"{row['日期']}: 收{row['收盘']:.2f} 高{row['最高']:.2f} 低{row['最低']:.2f}")
    result["after_kline_summary"] = "\n".join(kline_summary)

    return result


# ========== 模式C: 纯量化回测 ==========

def _quant_select_at_date(analysis_date: str, top_n: int = TOP_N) -> list:
    """
    纯量化选股（不调AI）：获取历史K线，用多因子评分直接选出TOP-N
    用于快速迭代验证因子效果
    """
    from BBBIG.stock_selector import calc_technical_indicators, _multifactor_score

    ref_date = datetime.strptime(analysis_date, "%Y%m%d")
    logger.info(f"[纯量化回测] 分析日期: {ref_date.strftime('%Y-%m-%d')}")

    # 获取当前活跃股列表
    all_stocks = fetcher.fetch_all_stocks()
    if all_stocks.empty:
        return []

    # 基础面过滤
    df = all_stocks.copy()
    from BBBIG.config import MIN_MARKET_CAP, MIN_VOLUME
    df = df[df["总市值"] >= MIN_MARKET_CAP]
    df = df[df["成交额"] >= MIN_VOLUME]
    df = df[(df["市盈率动"] > 0) & (df["市盈率动"] < 200)]
    df = df[df["市净率"] > 0]

    if df.empty:
        return []

    # 取前150只活跃股
    df = df.sort_values("成交额", ascending=False).head(150)
    codes = df["代码"].tolist()

    # 批量获取K线（分析日之前）
    kline_map = {}

    def _fetch_one(code):
        time.sleep(random.uniform(0.05, 0.2))
        return code, fetcher.fetch_stock_kline(code, days=KLINE_DAYS, end_date_str=analysis_date)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_one, c): c for c in codes[:100]}  # 限制数量
        for future in as_completed(futures):
            try:
                code, kline = future.result()
                if not kline.empty:
                    kline_map[code] = kline
            except Exception:
                pass

    if not kline_map:
        return []

    # 多因子评分
    scored = []
    hot_industries = set()  # 纯量化模式不用板块数据
    for _, row in df.iterrows():
        code = row['代码']
        kline = kline_map.get(code, pd.DataFrame())
        if kline.empty:
            continue
        ind = calc_technical_indicators(kline)
        score = _multifactor_score(row, ind, hot_industries)
        last_close = kline.iloc[-1]['收盘']
        scored.append({
            "code": code,
            "name": row['名称'],
            "industry": row.get('所处行业', ''),
            "current_price": last_close,
            "factor_score": score,
            # 简易目标价/止损价
            "suggested_buy_range": f"{last_close*0.98:.2f}-{last_close*1.01:.2f}",
            "target_price": round(last_close * 1.05, 2),
            "stop_loss": round(last_close * 0.95, 2),
        })

    # 按评分排序，取TOP-N
    scored.sort(key=lambda x: x["factor_score"], reverse=True)

    # 行业集中度控制
    from BBBIG.config import MAX_SAME_INDUSTRY
    industry_count = {}
    filtered = []
    for s in scored:
        ind = s.get("industry", "未知")
        cnt = industry_count.get(ind, 0)
        if cnt < MAX_SAME_INDUSTRY:
            filtered.append(s)
            industry_count[ind] = cnt + 1
        if len(filtered) >= top_n:
            break

    return filtered


def run_quant_backtest(weeks_list: list = None) -> dict:
    """
    执行纯量化回测（不调AI，快速验证因子效果）
    :param weeks_list: 回测周数列表，默认 [1, 2, 3, 4]
    :return: 完整回测报告
    """
    if weeks_list is None:
        weeks_list = [1, 2, 3, 4]

    logger.info("=" * 70)
    logger.info("BBBIG 纯量化回测开始（不调用AI，纯因子评分）")
    logger.info(f"回测周数: {weeks_list}")
    logger.info("=" * 70)

    report = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "quant_only",
        "backtest_weeks": weeks_list,
        "rounds": [],
        "summary": {}
    }

    total_recs = 0
    total_profit = 0
    total_loss = 0
    all_realized_pcts = []

    for weeks in weeks_list:
        analysis_date = _get_trade_date_before(weeks)
        ref_str = datetime.strptime(analysis_date, "%Y%m%d").strftime("%Y-%m-%d")
        logger.info(f"\n{'=' * 60}")
        logger.info(f"纯量化回测: {weeks}周前 (分析日: {ref_str})")
        logger.info("=" * 60)

        # 纯量化选股
        selected = _quant_select_at_date(analysis_date)
        if not selected:
            report["rounds"].append({
                "weeks_ago": weeks, "analysis_date": ref_str, "error": "无法选出候选股"
            })
            continue

        # 验证每只选出的股票
        evaluations = []
        for rec in selected:
            code = rec["code"]
            try:
                before_df, after_df = _fetch_kline_for_backtest(
                    code, analysis_date, days_before=KLINE_DAYS, days_after=10
                )
            except Exception:
                continue

            ev = _evaluate_recommendation(rec, after_df, before_df)
            evaluations.append(ev)

            status_mark = "✅" if "盈利" in ev.get("outcome", "") else (
                "❌" if "止损" in ev.get("outcome", "") else "➖")
            logger.info(f"  {status_mark} {code} {ev.get('name', '')}: {ev.get('outcome', '')} "
                        f"({ev.get('realized_pct', 0):+.2f}%) [因子分:{rec.get('factor_score',0):.1f}]")

        round_profit = sum(1 for e in evaluations if "盈利" in e.get("outcome", ""))
        round_loss = sum(1 for e in evaluations if "止损" in e.get("outcome", ""))
        pcts = [e.get("realized_pct", 0) for e in evaluations if "realized_pct" in e]

        round_summary = {
            "weeks_ago": weeks,
            "analysis_date": ref_str,
            "total_recommendations": len(evaluations),
            "profit_count": round_profit,
            "loss_count": round_loss,
            "neutral_count": len(evaluations) - round_profit - round_loss,
            "win_rate": f"{round_profit / len(evaluations) * 100:.1f}%" if evaluations else "0%",
            "avg_realized_pct": round(sum(pcts) / len(pcts), 2) if pcts else 0,
            "selected_stocks": [{"code": s["code"], "name": s["name"],
                                "factor_score": s["factor_score"]} for s in selected],
            "evaluations": evaluations
        }

        report["rounds"].append(round_summary)
        total_recs += len(evaluations)
        total_profit += round_profit
        total_loss += round_loss
        all_realized_pcts.extend(pcts)

    report["summary"] = {
        "total_recommendations": total_recs,
        "total_profit": total_profit,
        "total_loss": total_loss,
        "total_neutral": total_recs - total_profit - total_loss,
        "overall_win_rate": f"{total_profit / total_recs * 100:.1f}%" if total_recs > 0 else "0%",
        "avg_realized_pct": round(sum(all_realized_pcts) / len(all_realized_pcts), 2) if all_realized_pcts else 0,
    }

    # 保存结果
    fpath = os.path.join(RESULT_DIR, f"quant_backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    try:
        with open(fpath, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info(f"纯量化回测结果已保存至: {fpath}")
    except Exception as e:
        logger.error(f"保存回测结果异常: {e}")

    return report


# ========== 模式A: AI回测（原有逻辑，Prompt增强） ==========

def _run_selection_at_date(analysis_date: str, candidate_codes: list = None) -> dict:
    """模拟站在指定日期进行选股分析"""
    ref_date = datetime.strptime(analysis_date, "%Y%m%d")
    ref_str = ref_date.strftime("%Y-%m-%d")
    logger.info(f"[回测] 模拟分析日期: {ref_str}")

    if candidate_codes is None:
        logger.info("[回测] 获取当前活跃股列表作为候选池...")
        all_stocks = fetcher.fetch_all_stocks()
        if all_stocks.empty:
            return {"error": "获取A股行情失败"}
        from BBBIG.stock_selector import _prefilter_candidates
        # 先获取K线
        rough_codes = all_stocks.sort_values("成交额", ascending=False).head(100)["代码"].tolist()
        kline_map = {}
        for code in rough_codes[:60]:
            try:
                kline = fetcher.fetch_stock_kline(code, days=KLINE_DAYS, end_date_str=analysis_date)
                if not kline.empty:
                    kline_map[code] = kline
            except Exception:
                pass
        prefilter_result = _prefilter_candidates(all_stocks, kline_map, set(), top_n=60)
        if isinstance(prefilter_result, tuple):
            candidates, _ = prefilter_result
        else:
            candidates = prefilter_result
        candidate_codes = candidates["代码"].tolist()
        candidate_names = dict(zip(candidates["代码"], candidates["名称"]))
        candidate_industries = dict(zip(candidates["代码"], candidates["所处行业"]))
    else:
        candidate_names = {}
        candidate_industries = {}

    logger.info(f"[回测] 获取 {len(candidate_codes)} 只股票历史K线...")
    kline_before_map = {}
    kline_after_map = {}

    def _fetch_one(code):
        time.sleep(random.uniform(0.1, 0.4))
        before_df, after_df = _fetch_kline_for_backtest(
            code, analysis_date, days_before=KLINE_DAYS, days_after=10
        )
        return code, before_df, after_df

    with ThreadPoolExecutor(max_workers=4) as executor:
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
            f"  K线及技术指标:\n  {kline_text}"
        )

    system_prompt = """你是一位资深的A股量化分析师和投资顾问。请基于提供的市场数据和技术指标进行专业分析。
你需要综合考虑：
1. K线形态和均线排列（MA5/MA10/MA20多空排列）
2. MACD/KDJ/RSI等技术指标信号
3. 成交量变化和量价配合度
4. 近期动量和波动率

你的分析必须客观、专业，给出明确的买入区间、目标价和止损价。"""

    user_prompt = f"""请分析以下A股候选股票数据（K线截止日期为 {ref_str}），
从中选出最值得投资的前{TOP_N}支股票。

以下是候选股票（含K线和技术指标）：

{''.join(stock_summaries[:40])}

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
    analysis_result = deepseek.analyze_for_json(system_prompt, user_prompt, temperature=AI_TEMPERATURE, max_tokens=AI_MAX_TOKENS)

    recommendations = []
    if isinstance(analysis_result, dict) and "recommendations" in analysis_result:
        recommendations = analysis_result["recommendations"]

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
    """执行完整AI回测"""
    if weeks_list is None:
        weeks_list = [1, 2, 3]

    logger.info("=" * 70)
    logger.info("BBBIG AI回测验证开始")
    logger.info(f"回测周数: {weeks_list}")
    logger.info("=" * 70)

    report = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "ai",
        "backtest_weeks": weeks_list,
        "rounds": [],
        "summary": {}
    }

    total_recs = 0
    total_profit = 0
    total_loss = 0
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
                "weeks_ago": weeks, "analysis_date": analysis_date, "error": round_result["error"]
            })
            continue

        evaluations = round_result.get("evaluations", [])
        round_profit = sum(1 for e in evaluations if "盈利" in e.get("outcome", ""))
        round_loss = sum(1 for e in evaluations if "止损" in e.get("outcome", ""))
        pcts = [e.get("realized_pct", 0) for e in evaluations if "realized_pct" in e]

        round_summary = {
            "weeks_ago": weeks,
            "analysis_date": round_result["analysis_date"],
            "market_analysis": round_result.get("market_analysis", ""),
            "total_recommendations": len(evaluations),
            "profit_count": round_profit,
            "loss_count": round_loss,
            "neutral_count": len(evaluations) - round_profit - round_loss,
            "win_rate": f"{round_profit / len(evaluations) * 100:.1f}%" if evaluations else "0%",
            "avg_realized_pct": round(sum(pcts) / len(pcts), 2) if pcts else 0,
            "evaluations": evaluations
        }

        report["rounds"].append(round_summary)
        total_recs += len(evaluations)
        total_profit += round_profit
        total_loss += round_loss
        all_realized_pcts.extend(pcts)

    report["summary"] = {
        "total_recommendations": total_recs,
        "total_profit": total_profit,
        "total_loss": total_loss,
        "total_neutral": total_recs - total_profit - total_loss,
        "overall_win_rate": f"{total_profit / total_recs * 100:.1f}%" if total_recs > 0 else "0%",
        "overall_loss_rate": f"{total_loss / total_recs * 100:.1f}%" if total_recs > 0 else "0%",
        "target_hit_rate": "N/A",
        "stop_hit_rate": "N/A",
        "avg_realized_pct": round(sum(all_realized_pcts) / len(all_realized_pcts), 2) if all_realized_pcts else 0,
    }

    fpath = os.path.join(RESULT_DIR, f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    try:
        with open(fpath, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info(f"回测结果已保存至: {fpath}")
    except Exception as e:
        logger.error(f"保存回测结果异常: {e}")

    return report


# ========== 模式B: 推荐股回测 ==========

def backtest_stock_list(stock_list: list, weeks_list: list = None) -> dict:
    """对指定的股票列表进行多周回测验证"""
    if weeks_list is None:
        weeks_list = [1, 2, 3]

    logger.info("=" * 70)
    logger.info("BBBIG 推荐股票回测验证")
    logger.info(f"回测 {len(stock_list)} 只推荐股票，往前推 {weeks_list} 周")
    logger.info("=" * 70)

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

            kline_text = _format_kline_for_prompt(before_df)
            last_close = before_df.iloc[-1]["收盘"]

            system_prompt = """你是一位资深的A股量化分析师。请基于K线数据和技术指标给出精确的交易建议。
重点关注：均线排列、MACD信号、RSI超买超卖、成交量趋势。"""

            user_prompt = f"""请分析以下股票的K线数据和技术指标（截止 {ref_str}），给出交易建议。

【{code} {name}】 当前价: {last_close:.2f}
K线及技术指标:
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
  "reason": "基于技术指标的分析理由（80字以内）"
}}
```"""

            logger.info(f"  [{weeks}周前 {ref_str}] 调用 DeepSeek 分析...")
            analysis_result = deepseek.analyze_for_json(system_prompt, user_prompt, temperature=AI_TEMPERATURE, max_tokens=1024)

            if not isinstance(analysis_result, dict) or "suggested_buy_range" not in analysis_result:
                logger.warning(f"  大模型返回格式异常，使用原始推荐数据")
                analysis_result = {
                    "code": code, "name": name,
                    "suggested_buy_range": rec.get("suggested_buy_range", ""),
                    "target_price": rec.get("target_price", 0),
                    "stop_loss": rec.get("stop_loss", 0),
                }

            ev = _evaluate_recommendation(analysis_result, after_df, before_df)
            outcome = ev.get("outcome", "")

            round_info = {
                "weeks_ago": weeks, "date": ref_str,
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
            "code": code, "name": name,
            "industry": rec.get("industry", ""),
            "current_price": rec.get("current_price", 0),
            "reason": rec.get("reason", ""),
            "suggested_buy_range": rec.get("suggested_buy_range", ""),
            "target_price": rec.get("target_price", ""),
            "stop_loss": rec.get("stop_loss", ""),
            "win_count": win_count, "loss_count": loss_count,
            "neutral_count": neutral_count, "total_rounds": total_rounds,
            "win_rate": round(win_rate, 1),
            "avg_profit_pct": round(avg_pct, 2),
            "rounds": rounds,
        })

    stock_results.sort(key=lambda x: (x["win_rate"], x["avg_profit_pct"]), reverse=True)
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

    fpath = os.path.join(RESULT_DIR, f"backtest_ranked_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    try:
        with open(fpath, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info(f"回测排序结果已保存至: {fpath}")
    except Exception as e:
        logger.error(f"保存回测排序结果异常: {e}")

    return report


# ========== 报告格式化 ==========

def format_backtest_report(report: dict) -> str:
    """将回测结果格式化为可读报告"""
    lines = []
    mode = report.get("mode", "ai")
    mode_label = "纯量化" if mode == "quant_only" else "AI"

    lines.append("=" * 70)
    lines.append(f"  BBBIG {mode_label}回测验证报告  {report['timestamp']}")
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

        # 纯量化模式显示因子评分
        if mode == "quant_only" and rd.get("selected_stocks"):
            lines.append("  选股因子评分:")
            for s in rd["selected_stocks"]:
                lines.append(f"    {s['code']} {s['name']} → 因子分: {s['factor_score']:.1f}")
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
            lines.append(f"     买入价(中值): {ev['buy_price']:.2f}")
            lines.append(f"     后续最高: {ev['max_price']:.2f} ({ev['max_profit_pct']:+.2f}%)  |  "
                         f"后续最低: {ev['min_price']:.2f} ({ev['max_loss_pct']:+.2f}%)")
            lines.append(f"     期末收盘: {ev['end_price']:.2f} ({ev['final_profit_pct']:+.2f}%)")
            lines.append(f"     结果: {outcome}  |  实际收益: {ev.get('realized_pct', 0):+.2f}%")
            lines.append("")

    summary = report.get("summary", {})
    lines.append("=" * 70)
    lines.append("  📊 回测汇总")
    lines.append("=" * 70)
    lines.append(f"  总推荐数: {summary.get('total_recommendations', 0)}")
    lines.append(f"  盈利数: {summary.get('total_profit', 0)}  |  止损数: {summary.get('total_loss', 0)}  |  "
                 f"未触发: {summary.get('total_neutral', 0)}")
    lines.append(f"  总胜率: {summary.get('overall_win_rate', '0%')}")
    lines.append(f"  平均实际收益: {summary.get('avg_realized_pct', 0):+.2f}%")
    lines.append("")
    lines.append("  ⚠ 免责声明：回测结果仅供参考，历史表现不代表未来收益。")
    lines.append("=" * 70)
    return "\n".join(lines)


def format_ranked_backtest_report(report: dict) -> str:
    """将按盈利概率排序的回测结果格式化"""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  BBBIG 选股推荐 × 回测验证报告  {report['timestamp']}")
    lines.append(f"  回测方式: 往前推 {report['backtest_weeks']} 周")
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

    lines.append(f"\n{'=' * 70}")
    lines.append("  各股票回测明细")
    lines.append("=" * 70)

    for sr in report.get("stock_results", []):
        lines.append(f"\n  #{sr['rank']} {sr['code']} {sr['name']} "
                     f"— 盈利概率 {sr['win_rate']:.1f}% | 平均收益 {sr['avg_profit_pct']:+.2f}%")
        if sr.get("reason"):
            lines.append(f"  推荐理由: {sr['reason']}")

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
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "ai"

    if mode == "quant":
        report = run_quant_backtest([1, 2, 3, 4])
        print(format_backtest_report(report))
    else:
        report = run_backtest([1, 2, 3])
        print(format_backtest_report(report))
