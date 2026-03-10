#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能选股模块
借助 DeepSeek 大模型，综合分析A股热点、K线、交易量，筛选最值得投资的前10支股票
"""
import logging
import time
import random
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from BBBIG.data_fetcher import fetcher
from BBBIG.deepseek_client import deepseek
from BBBIG.config import TOP_N, KLINE_DAYS, MIN_MARKET_CAP, MIN_VOLUME

logger = logging.getLogger("BBBIG")


def _format_kline_summary(kline_df: pd.DataFrame) -> str:
    """将K线 DataFrame 压缩为文本摘要（最近一周）"""
    if kline_df.empty:
        return "无数据"
    # 取最近5个交易日
    recent = kline_df.tail(5)
    lines = []
    for _, row in recent.iterrows():
        lines.append(
            f"{row['日期']}: 开{row['开盘']:.2f} 高{row['最高']:.2f} "
            f"低{row['最低']:.2f} 收{row['收盘']:.2f} "
            f"量{row['成交量']:.0f} 额{row['成交额']:.0f} "
            f"涨跌幅{row['涨跌幅']:.2f}% 换手{row['换手率']:.2f}%"
        )
    # 计算周涨跌幅
    if len(kline_df) >= 5:
        week_start = kline_df.iloc[-5]["收盘"]
        week_end = kline_df.iloc[-1]["收盘"]
        week_change = (week_end - week_start) / week_start * 100
        lines.append(f"近一周涨跌幅: {week_change:.2f}%")
    # 计算平均成交量变化
    if len(kline_df) >= 10:
        avg_vol_prev = kline_df.iloc[-10:-5]["成交量"].mean()
        avg_vol_recent = kline_df.iloc[-5:]["成交量"].mean()
        if avg_vol_prev > 0:
            vol_ratio = avg_vol_recent / avg_vol_prev
            lines.append(f"近一周成交量相比上周变化: {vol_ratio:.2f}倍")
    return "\n".join(lines)


def _prefilter_candidates(all_stocks: pd.DataFrame, top_n: int = 100) -> pd.DataFrame:
    """
    基础面预筛选：从全量A股中筛选出候选股票池
    筛选条件：
    - 总市值 >= MIN_MARKET_CAP
    - 成交额 >= MIN_VOLUME
    - 市盈率 > 0（盈利）
    - 涨跌幅在 -5% ~ 8% 之间（排除极端涨跌）
    按综合评分排序取 top_n
    """
    df = all_stocks.copy()
    df = df[df["总市值"] >= MIN_MARKET_CAP]
    df = df[df["成交额"] >= MIN_VOLUME]
    df = df[(df["市盈率动"] > 0) & (df["市盈率动"] < 200)]
    df = df[(df["涨跌幅"] > -5) & (df["涨跌幅"] < 8)]
    df = df[df["市净率"] > 0]

    if df.empty:
        return df

    # 综合评分：换手率适中 + 量比较大 + 涨幅适中
    df = df.copy()
    df["score"] = (
        df["量比"].clip(0, 10) * 2 +
        df["换手率"].clip(0, 20) * 1.5 +
        df["涨跌幅"].clip(-3, 5) * 1 +
        (df["成交额"] / 1e8).clip(0, 50) * 0.5
    )
    df = df.sort_values("score", ascending=False).head(top_n)
    return df.reset_index(drop=True)


def _fetch_kline_batch(codes: list, days: int = KLINE_DAYS) -> dict:
    """批量获取K线数据"""
    kline_map = {}

    def _fetch_one(code):
        time.sleep(random.uniform(0.1, 0.5))
        return code, fetcher.fetch_stock_kline(code, days=days)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_one, code): code for code in codes}
        for future in as_completed(futures):
            try:
                code, kline = future.result()
                if not kline.empty:
                    kline_map[code] = kline
            except Exception as e:
                logger.warning(f"获取K线异常: {e}")
    return kline_map


def run_stock_selection() -> dict:
    """
    执行智能选股流程
    返回: {"timestamp": ..., "hot_sectors": ..., "recommendations": [...], "analysis": "..."}
    """
    logger.info("=" * 60)
    logger.info("开始执行智能选股...")
    result = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hot_sectors": "",
        "recommendations": [],
        "analysis": ""
    }

    # Step 1: 获取行业和概念板块资金流向
    logger.info("[1/5] 获取板块资金流向...")
    hot_sectors = fetcher.fetch_hot_sectors()
    concept_sectors = fetcher.fetch_concept_sectors()

    sector_text = "【行业板块资金流向 TOP20】\n"
    if not hot_sectors.empty:
        for _, row in hot_sectors.iterrows():
            sector_text += f"  {row['板块名称']}: 涨跌幅{row['涨跌幅']:.2f}%, 主力净流入{row['主力净流入']/1e8:.2f}亿\n"

    concept_text = "【概念板块资金流向 TOP20】\n"
    if not concept_sectors.empty:
        for _, row in concept_sectors.iterrows():
            concept_text += f"  {row['板块名称']}: 涨跌幅{row['涨跌幅']:.2f}%, 主力净流入{row['主力净流入']/1e8:.2f}亿\n"

    result["hot_sectors"] = sector_text + "\n" + concept_text

    # Step 2: 获取全量A股行情
    logger.info("[2/5] 获取A股实时行情...")
    all_stocks = fetcher.fetch_all_stocks()
    if all_stocks.empty:
        logger.error("获取A股行情失败")
        result["analysis"] = "错误：无法获取A股行情数据"
        return result
    logger.info(f"  共获取 {len(all_stocks)} 只A股")

    # Step 3: 基础面预筛选
    logger.info("[3/5] 基础面预筛选候选股票...")
    candidates = _prefilter_candidates(all_stocks, top_n=80)
    if candidates.empty:
        logger.error("预筛选后无候选股票")
        result["analysis"] = "错误：预筛选后无候选股票"
        return result
    logger.info(f"  预筛选后 {len(candidates)} 只候选股票")

    # Step 4: 批量获取候选股K线
    logger.info("[4/5] 批量获取候选股K线数据...")
    candidate_codes = candidates["代码"].tolist()
    kline_map = _fetch_kline_batch(candidate_codes, days=KLINE_DAYS)
    logger.info(f"  成功获取 {len(kline_map)} 只股票K线")

    # Step 5: 构造大模型 prompt 进行分析
    logger.info("[5/5] 调用 DeepSeek 大模型进行综合分析...")

    # 构造候选股数据摘要
    stock_summaries = []
    for _, row in candidates.iterrows():
        code = row["代码"]
        kline_text = _format_kline_summary(kline_map.get(code, pd.DataFrame()))
        stock_summaries.append(
            f"【{code} {row['名称']}】 行业:{row['所处行业']} "
            f"最新价:{row['最新价']:.2f} 涨跌幅:{row['涨跌幅']:.2f}% "
            f"成交额:{row['成交额']/1e8:.2f}亿 换手率:{row['换手率']:.2f}% "
            f"市盈率:{row['市盈率动']:.1f} 市净率:{row['市净率']:.2f} "
            f"总市值:{row['总市值']/1e8:.0f}亿 量比:{row['量比']:.2f}\n"
            f"  K线数据:\n  {kline_text}"
        )

    system_prompt = """你是一位资深的A股量化分析师和投资顾问。请基于提供的市场数据进行专业分析。
你需要综合考虑以下因素：
1. 当前市场热点板块和资金流向趋势
2. 个股的K线形态（是否有上升趋势、支撑位是否稳固）
3. 成交量变化（是否放量、量价配合是否合理）
4. 基本面指标（市盈率、市净率是否合理）
5. 技术面信号（均线趋势、MACD、KDJ等隐含信号）

你的分析必须客观、专业，给出明确的投资建议。"""

    user_prompt = f"""请分析以下A股市场数据，从候选股票中选出最值得投资的前{TOP_N}支股票。

当前日期: {datetime.now().strftime('%Y-%m-%d')}

{result['hot_sectors']}

以下是经过初步筛选的候选股票（含最近一周K线数据）：

{''.join(stock_summaries[:40])}

请完成以下分析任务：
1. **市场热点分析**：基于板块资金流向，分析当前A股市场热点和趋势方向
2. **选股推荐**：综合K线趋势、成交量变化、基本面等因素，选出最值得投资的前{TOP_N}支股票
3. **逐一分析**：对每只推荐股票给出具体的买入理由

请严格按以下JSON格式返回：
```json
{{
  "market_analysis": "对当前A股市场热点和趋势的分析（200字以内）",
  "recommendations": [
    {{
      "rank": 1,
      "code": "股票代码",
      "name": "股票名称",
      "industry": "所属行业",
      "current_price": 当前价格,
      "reason": "推荐理由（含K线分析、量能分析、基本面分析，150字以内）",
      "suggested_buy_range": "建议买入区间（如 12.5-13.0）",
      "target_price": "短期目标价",
      "stop_loss": "止损价"
    }}
  ]
}}
```"""

    analysis_result = deepseek.analyze_for_json(system_prompt, user_prompt, temperature=0.3, max_tokens=4096)

    if isinstance(analysis_result, dict):
        if "recommendations" in analysis_result:
            result["recommendations"] = analysis_result["recommendations"]
            result["analysis"] = analysis_result.get("market_analysis", "")
        elif "raw_response" in analysis_result:
            result["analysis"] = analysis_result["raw_response"]
        else:
            result["analysis"] = str(analysis_result)
    else:
        result["analysis"] = str(analysis_result)

    logger.info(f"选股完成，推荐 {len(result['recommendations'])} 只股票")

    # 自动对推荐股票执行回测验证，按盈利概率排序
    if result["recommendations"]:
        logger.info("开始对推荐股票进行回测验证...")
        from BBBIG.backtester import backtest_stock_list
        backtest_report = backtest_stock_list(result["recommendations"], weeks_list=[1, 2, 3])
        result["backtest_report"] = backtest_report
        # 按盈利概率重新排序 recommendations
        if backtest_report.get("stock_results"):
            ranked_codes = [sr["code"] for sr in backtest_report["stock_results"]]
            rec_map = {r["code"]: r for r in result["recommendations"]}
            sorted_recs = []
            for i, code in enumerate(ranked_codes):
                if code in rec_map:
                    rec = rec_map[code]
                    rec["rank"] = i + 1
                    # 附加回测数据
                    sr = next((s for s in backtest_report["stock_results"] if s["code"] == code), None)
                    if sr:
                        rec["backtest_win_rate"] = sr["win_rate"]
                        rec["backtest_avg_profit"] = sr["avg_profit_pct"]
                        rec["backtest_rounds"] = sr["rounds"]
                    sorted_recs.append(rec)
            result["recommendations"] = sorted_recs
            logger.info("推荐股票已按回测盈利概率重新排序")

    return result


def format_selection_report(result: dict) -> str:
    """将选股结果格式化为可读报告（含回测验证）"""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  BBBIG 智能选股报告  {result['timestamp']}")
    lines.append("=" * 70)

    if result.get("hot_sectors"):
        lines.append("\n" + result["hot_sectors"])

    if result.get("analysis"):
        lines.append("\n【市场热点分析】")
        lines.append(result["analysis"])

    if result.get("recommendations"):
        has_backtest = any("backtest_win_rate" in r for r in result["recommendations"])

        lines.append(f"\n{'=' * 70}")
        if has_backtest:
            lines.append(f"  推荐股票 TOP {len(result['recommendations'])}（按回测盈利概率排序）")
        else:
            lines.append(f"  推荐股票 TOP {len(result['recommendations'])}")
        lines.append("=" * 70)

        if has_backtest:
            lines.append(f"\n  {'排名':<4} {'代码':<8} {'名称':<8} {'盈利概率':>8} "
                         f"{'平均收益':>8} {'买入区间':>14} {'目标价':>8} {'止损价':>8}")
            lines.append("─" * 70)
            for rec in result["recommendations"]:
                lines.append(
                    f"  #{rec.get('rank', '?'):<3} {rec.get('code', ''):<8} "
                    f"{rec.get('name', ''):<8} "
                    f"{rec.get('backtest_win_rate', 0):>6.1f}% "
                    f"{rec.get('backtest_avg_profit', 0):>+7.2f}% "
                    f"{str(rec.get('suggested_buy_range', '')):>14} "
                    f"{str(rec.get('target_price', '')):>8} "
                    f"{str(rec.get('stop_loss', '')):>8}"
                )
            lines.append("─" * 70)

        lines.append("")
        for rec in result["recommendations"]:
            lines.append(f"\n  #{rec.get('rank', '?')} {rec.get('code', '')} {rec.get('name', '')}")
            lines.append(f"  行业: {rec.get('industry', '-')}")
            lines.append(f"  当前价: {rec.get('current_price', '-')}")
            lines.append(f"  推荐理由: {rec.get('reason', '-')}")
            lines.append(f"  建议买入区间: {rec.get('suggested_buy_range', '-')}")
            lines.append(f"  短期目标价: {rec.get('target_price', '-')}")
            lines.append(f"  止损价: {rec.get('stop_loss', '-')}")

            if "backtest_win_rate" in rec:
                lines.append(f"  📊 回测盈利概率: {rec['backtest_win_rate']:.1f}% | "
                             f"平均收益: {rec.get('backtest_avg_profit', 0):+.2f}%")
                for rd in rec.get("backtest_rounds", []):
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
            lines.append("-" * 50)

        # 回测汇总
        if result.get("backtest_report"):
            bt_summary = result["backtest_report"].get("summary", {})
            lines.append(f"\n{'=' * 70}")
            lines.append("  📊 回测验证汇总")
            lines.append("=" * 70)
            lines.append(f"  回测方式: 往前推 {result['backtest_report'].get('backtest_weeks', [])} 周")
            lines.append(f"  总回测轮数: {bt_summary.get('total_rounds', 0)} | "
                         f"总盈利: {bt_summary.get('total_wins', 0)} | "
                         f"总止损: {bt_summary.get('total_losses', 0)}")
            lines.append(f"  总胜率: {bt_summary.get('overall_win_rate', '0%')} | "
                         f"平均收益: {bt_summary.get('avg_profit_pct', 0):+.2f}%")
    else:
        lines.append("\n暂无推荐股票")

    lines.append("\n⚠ 免责声明：以上分析仅供参考，不构成投资建议。投资有风险，入市需谨慎。")
    lines.append("=" * 70)
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = run_stock_selection()
    report = format_selection_report(result)
    print(report)
