#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓分析模块
对用户持有的股票进行大模型分析，预测卖出点和止损点
"""
import logging
import time
import random
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from BBBIG.data_fetcher import fetcher
from BBBIG.deepseek_client import deepseek
from BBBIG.config import load_portfolio, save_portfolio, KLINE_DAYS, PORTFOLIO_FILE

logger = logging.getLogger("BBBIG")


def add_holding(code: str, cost: float, shares: int = 0, name: str = ""):
    """
    添加持仓股票
    :param code: 股票代码
    :param cost: 成本价（每股）
    :param shares: 持有股数（可选）
    :param name: 股票名称（可选，自动获取）
    """
    portfolio = load_portfolio()
    # 检查是否已存在
    for item in portfolio:
        if item["code"] == code:
            item["cost"] = cost
            if shares > 0:
                item["shares"] = shares
            if name:
                item["name"] = name
            save_portfolio(portfolio)
            logger.info(f"更新持仓: {code} 成本价={cost}")
            return
    portfolio.append({
        "code": code,
        "name": name,
        "cost": cost,
        "shares": shares,
        "add_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    save_portfolio(portfolio)
    logger.info(f"添加持仓: {code} 成本价={cost}")


def remove_holding(code: str):
    """移除持仓股票"""
    portfolio = load_portfolio()
    portfolio = [item for item in portfolio if item["code"] != code]
    save_portfolio(portfolio)
    logger.info(f"移除持仓: {code}")


def list_holdings() -> list:
    """列出所有持仓"""
    return load_portfolio()


def _format_kline_detail(kline_df, code: str, name: str, cost: float) -> str:
    """格式化K线详情用于大模型分析"""
    if kline_df.empty:
        return f"【{code} {name}】无K线数据"

    lines = [f"【{code} {name}】成本价: {cost:.2f}"]

    # 最近一周K线详情
    recent_week = kline_df.tail(5)
    lines.append("最近一周K线:")
    for _, row in recent_week.iterrows():
        lines.append(
            f"  {row['日期']}: 开{row['开盘']:.2f} 高{row['最高']:.2f} "
            f"低{row['最低']:.2f} 收{row['收盘']:.2f} "
            f"量{row['成交量']:.0f} 额{row['成交额']:.0f} "
            f"涨跌幅{row['涨跌幅']:.2f}% 换手{row['换手率']:.2f}%"
        )

    # 当前价格与成本对比
    current_price = kline_df.iloc[-1]["收盘"]
    profit_pct = (current_price - cost) / cost * 100
    lines.append(f"当前价: {current_price:.2f}, 持仓盈亏: {profit_pct:+.2f}%")

    # 近30天K线概要
    if len(kline_df) >= 20:
        lines.append(f"\n近30天K线概要:")
        month_data = kline_df.tail(20)
        for _, row in month_data.iterrows():
            lines.append(
                f"  {row['日期']}: 收{row['收盘']:.2f} 涨跌{row['涨跌幅']:.2f}% "
                f"量{row['成交量']:.0f} 换手{row['换手率']:.2f}%"
            )

    # 统计指标
    if len(kline_df) >= 5:
        week_high = kline_df.tail(5)["最高"].max()
        week_low = kline_df.tail(5)["最低"].min()
        week_avg_vol = kline_df.tail(5)["成交量"].mean()
        lines.append(f"一周最高: {week_high:.2f}, 一周最低: {week_low:.2f}")
        lines.append(f"一周平均成交量: {week_avg_vol:.0f}")

    if len(kline_df) >= 20:
        ma5 = kline_df.tail(5)["收盘"].mean()
        ma10 = kline_df.tail(10)["收盘"].mean()
        ma20 = kline_df.tail(20)["收盘"].mean()
        lines.append(f"MA5: {ma5:.2f}, MA10: {ma10:.2f}, MA20: {ma20:.2f}")

        # 简易MACD信号
        ema12 = kline_df["收盘"].ewm(span=12).mean().iloc[-1]
        ema26 = kline_df["收盘"].ewm(span=26).mean().iloc[-1]
        dif = ema12 - ema26
        lines.append(f"EMA12: {ema12:.2f}, EMA26: {ema26:.2f}, DIF: {dif:.4f}")

    return "\n".join(lines)


def run_portfolio_analysis() -> dict:
    """
    执行持仓分析
    返回: {"timestamp": ..., "holdings_analysis": [...]}
    """
    logger.info("=" * 60)
    logger.info("开始执行持仓分析...")
    result = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "holdings_analysis": []
    }

    portfolio = load_portfolio()
    if not portfolio:
        logger.info("暂无持仓股票，跳过分析")
        result["holdings_analysis"] = []
        return result

    logger.info(f"共 {len(portfolio)} 只持仓股票")

    # 获取持仓股K线数据
    kline_map = {}

    def _fetch_one(item):
        time.sleep(random.uniform(0.1, 0.5))
        code = item["code"]
        kline = fetcher.fetch_stock_kline(code, days=KLINE_DAYS)
        return code, kline

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_one, item): item for item in portfolio}
        for future in as_completed(futures):
            try:
                code, kline = future.result()
                kline_map[code] = kline
            except Exception as e:
                logger.warning(f"获取持仓K线异常: {e}")

    # 获取股票名称（如果持仓中没有的话）
    all_stocks = fetcher.fetch_all_stocks()
    name_map = {}
    if not all_stocks.empty:
        name_map = dict(zip(all_stocks["代码"], all_stocks["名称"]))

    # 对每只持仓股调用大模型分析
    for item in portfolio:
        code = item["code"]
        cost = item["cost"]
        name = item.get("name", "") or name_map.get(code, code)
        item["name"] = name  # 回填名称

        kline = kline_map.get(code, None)
        if kline is None or kline.empty:
            result["holdings_analysis"].append({
                "code": code, "name": name, "cost": cost,
                "analysis": "无法获取K线数据，跳过分析"
            })
            continue

        current_price = kline.iloc[-1]["收盘"]
        profit_pct = (current_price - cost) / cost * 100

        kline_detail = _format_kline_detail(kline, code, name, cost)

        system_prompt = """你是一位资深的A股技术分析师和风险管理专家。
请基于提供的K线数据和持仓信息，给出专业的操作建议。
你需要重点分析：
1. 最近一周的K线走势趋势（是上涨、下跌还是震荡）
2. 成交量变化是否异常
3. 关键支撑位和压力位
4. 均线系统的多空排列
5. 基于持仓成本的盈亏状况

给出明确的操作建议：继续持有、分批卖出、止损清仓、或加仓。"""

        user_prompt = f"""请分析以下持仓股票的近期走势，并给出操作建议：

当前日期: {datetime.now().strftime('%Y-%m-%d')}

{kline_detail}

请严格按以下JSON格式返回：
```json
{{
  "code": "{code}",
  "name": "{name}",
  "current_price": {current_price:.2f},
  "cost": {cost:.2f},
  "profit_pct": {profit_pct:.2f},
  "trend": "上涨/下跌/震荡",
  "trend_analysis": "对最近一周K线趋势的详细分析（200字以内）",
  "volume_analysis": "对成交量变化的分析（100字以内）",
  "support_level": "关键支撑位价格",
  "resistance_level": "关键压力位价格",
  "action": "持有/分批卖出/止损/加仓",
  "action_reason": "操作建议的理由（150字以内）",
  "sell_point": "建议卖出价位（或区间）",
  "stop_loss": "建议止损价位",
  "risk_level": "低/中/高"
}}
```"""

        logger.info(f"  分析 {code} {name}...")
        analysis = deepseek.analyze_for_json(system_prompt, user_prompt, temperature=0.2, max_tokens=2048)

        if isinstance(analysis, dict) and "code" in analysis:
            result["holdings_analysis"].append(analysis)
        else:
            result["holdings_analysis"].append({
                "code": code, "name": name, "cost": cost,
                "current_price": current_price,
                "profit_pct": round(profit_pct, 2),
                "analysis": analysis.get("raw_response", str(analysis)) if isinstance(analysis, dict) else str(analysis)
            })

    # 更新持仓名称
    save_portfolio(portfolio)

    logger.info(f"持仓分析完成，共分析 {len(result['holdings_analysis'])} 只股票")
    return result


def format_portfolio_report(result: dict) -> str:
    """将持仓分析结果格式化为可读报告"""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  BBBIG 持仓分析报告  {result['timestamp']}")
    lines.append("=" * 70)

    if not result.get("holdings_analysis"):
        lines.append("\n暂无持仓股票。")
        lines.append(f"\n请编辑 {PORTFOLIO_FILE} 添加持仓，或使用命令行添加：")
        lines.append('  python -m BBBIG.main add 000001 15.50 1000')
        lines.append("=" * 70)
        return "\n".join(lines)

    for item in result["holdings_analysis"]:
        code = item.get("code", "")
        name = item.get("name", "")
        lines.append(f"\n  {code} {name}")
        lines.append("-" * 50)

        if "trend" in item:
            cost = item.get("cost", 0)
            current = item.get("current_price", 0)
            profit = item.get("profit_pct", 0)
            lines.append(f"  成本价: {cost}  |  当前价: {current}  |  盈亏: {profit:+.2f}%")
            lines.append(f"  趋势: {item.get('trend', '-')}")
            lines.append(f"  趋势分析: {item.get('trend_analysis', '-')}")
            lines.append(f"  量能分析: {item.get('volume_analysis', '-')}")
            lines.append(f"  支撑位: {item.get('support_level', '-')}  |  压力位: {item.get('resistance_level', '-')}")
            lines.append(f"  ★ 操作建议: {item.get('action', '-')}")
            lines.append(f"    理由: {item.get('action_reason', '-')}")
            lines.append(f"  建议卖出点: {item.get('sell_point', '-')}")
            lines.append(f"  止损价位: {item.get('stop_loss', '-')}")
            lines.append(f"  风险等级: {item.get('risk_level', '-')}")
        elif "analysis" in item:
            lines.append(f"  分析: {item['analysis']}")

    lines.append("\n⚠ 免责声明：以上分析仅供参考，不构成投资建议。投资有风险，入市需谨慎。")
    lines.append("=" * 70)
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # 示例：添加持仓并分析
    portfolio = load_portfolio()
    if not portfolio:
        print("暂无持仓，添加示例持仓...")
        add_holding("000001", 12.50, 1000, "平安银行")
        add_holding("600519", 1700.00, 100, "贵州茅台")

    result = run_portfolio_analysis()
    report = format_portfolio_report(result)
    print(report)
