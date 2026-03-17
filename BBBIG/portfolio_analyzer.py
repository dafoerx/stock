#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓分析模块（优化版）
对用户持有的股票进行大模型分析，增强技术指标，预测卖出点和止损点
"""
import logging
import time
import random
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from BBBIG.data_fetcher import fetcher
from BBBIG.deepseek_client import deepseek
from BBBIG.config import load_portfolio, save_portfolio, KLINE_DAYS, PORTFOLIO_FILE, AI_TEMPERATURE
from BBBIG.stock_selector import calc_technical_indicators, calc_ma

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


def _format_kline_detail_enhanced(kline_df, code: str, name: str, cost: float) -> str:
    """格式化K线详情用于大模型分析（增强版，含完整技术指标）"""
    if kline_df.empty:
        return f"【{code} {name}】无K线数据"

    lines = [f"【{code} {name}】成本价: {cost:.2f}"]

    current_price = kline_df.iloc[-1]["收盘"]
    profit_pct = (current_price - cost) / cost * 100
    lines.append(f"当前价: {current_price:.2f}, 持仓盈亏: {profit_pct:+.2f}%")

    # 最近一周K线详情
    recent_week = kline_df.tail(5)
    lines.append("\n最近一周K线:")
    for _, row in recent_week.iterrows():
        lines.append(
            f"  {row['日期']}: 开{row['开盘']:.2f} 高{row['最高']:.2f} "
            f"低{row['最低']:.2f} 收{row['收盘']:.2f} "
            f"量{row['成交量']:.0f} 额{row['成交额']:.0f} "
            f"涨跌幅{row['涨跌幅']:.2f}% 换手{row['换手率']:.2f}%"
        )

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
        lines.append(f"\n一周最高: {week_high:.2f}, 一周最低: {week_low:.2f}")

    # 完整技术指标
    indicators = calc_technical_indicators(kline_df)
    if indicators:
        lines.append(f"\n--- 技术指标 ---")
        lines.append(f"MA5: {indicators.get('ma5',0):.2f} MA10: {indicators.get('ma10',0):.2f} MA20: {indicators.get('ma20',0):.2f}"
                     f" {'多头排列' if indicators.get('ma_bull') else '空头排列' if indicators.get('ma_bear') else '交叉'}")
        lines.append(f"MACD: DIF={indicators.get('macd_dif',0):.4f} DEA={indicators.get('macd_dea',0):.4f} "
                     f"信号={indicators.get('macd_signal','未知')}")
        lines.append(f"KDJ: K={indicators.get('kdj_k',50):.1f} D={indicators.get('kdj_d',50):.1f} "
                     f"J={indicators.get('kdj_j',50):.1f} 信号={indicators.get('kdj_signal','未知')}")
        lines.append(f"RSI(14): {indicators.get('rsi',50):.1f}")
        lines.append(f"布林带位置: {indicators.get('boll_pos',0.5):.2f} (0=下轨, 0.5=中轨, 1=上轨)")
        lines.append(f"ATR波动率: {indicators.get('atr_pct',0):.2f}%")
        lines.append(f"近5日涨幅: {indicators.get('chg_5d',0):.2f}% 近10日涨幅: {indicators.get('chg_10d',0):.2f}%")
        lines.append(f"成交量趋势: {indicators.get('vol_trend',1):.2f}倍 量价相关: {indicators.get('vol_price_corr',0):.2f}")
        lines.append(f"距20日高点: {indicators.get('dist_high_20d',0):.2f}%")

    # 关键支撑/压力位估算
    if len(kline_df) >= 20:
        close = kline_df['收盘']
        ma20_val = calc_ma(close, 20).iloc[-1]
        recent_low = kline_df.tail(10)["最低"].min()
        recent_high = kline_df.tail(10)["最高"].max()
        lines.append(f"\n参考支撑位: MA20={ma20_val:.2f}, 近10日低点={recent_low:.2f}")
        lines.append(f"参考压力位: 近10日高点={recent_high:.2f}")
        # 成本价相对位置
        lines.append(f"成本价相对当前价位: {'盈利' if current_price > cost else '亏损'} {profit_pct:+.2f}%")
        if cost > current_price:
            lines.append(f"回本需涨: {(cost/current_price - 1)*100:.2f}%")

    return "\n".join(lines)


def run_portfolio_analysis() -> dict:
    """
    执行持仓分析（增强版）
    返回: {"timestamp": ..., "holdings_analysis": [...]}
    """
    logger.info("=" * 60)
    logger.info("开始执行持仓分析（增强版）...")
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

    # 获取股票名称
    all_stocks = fetcher.fetch_all_stocks()
    name_map = {}
    if not all_stocks.empty:
        name_map = dict(zip(all_stocks["代码"], all_stocks["名称"]))

    # 对每只持仓股调用大模型分析
    for item in portfolio:
        code = item["code"]
        cost = item["cost"]
        name = item.get("name", "") or name_map.get(code, code)
        item["name"] = name

        kline = kline_map.get(code, None)
        if kline is None or kline.empty:
            result["holdings_analysis"].append({
                "code": code, "name": name, "cost": cost,
                "analysis": "无法获取K线数据，跳过分析"
            })
            continue

        current_price = kline.iloc[-1]["收盘"]
        profit_pct = (current_price - cost) / cost * 100

        kline_detail = _format_kline_detail_enhanced(kline, code, name, cost)

        system_prompt = """你是一位资深的A股技术分析师和风险管理专家。
请基于提供的K线数据、技术指标和持仓信息，给出专业的操作建议。

你必须基于以下技术指标进行系统分析：
1. **均线系统**: MA5/MA10/MA20 的多空排列，价格相对均线位置
2. **MACD**: DIF/DEA 的金叉死叉，柱状图方向
3. **KDJ**: K/D/J 超买超卖区间，金叉死叉
4. **RSI**: 超买(>70)超卖(<30)判断
5. **布林带**: 价格在布林带中的位置（上中下轨）
6. **成交量**: 量能趋势、量价配合度
7. **ATR波动率**: 当前波动水平
8. **支撑/压力位**: 结合均线和近期高低点

基于以上指标给出明确的操作建议：继续持有、分批卖出、止损清仓、或加仓。
每个建议必须有具体的技术指标支撑。"""

        user_prompt = f"""请分析以下持仓股票的近期走势和完整技术指标，并给出操作建议：

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
  "trend_analysis": "基于均线系统和MACD的趋势详细分析（200字以内）",
  "volume_analysis": "基于成交量趋势和量价配合度的分析（100字以内）",
  "technical_summary": "KDJ/RSI/布林带综合技术面判断（100字以内）",
  "support_level": "关键支撑位价格（基于MA和近期低点）",
  "resistance_level": "关键压力位价格（基于近期高点和布林上轨）",
  "action": "持有/分批卖出/止损/加仓",
  "action_reason": "操作建议的理由，必须引用具体技术指标数值（200字以内）",
  "sell_point": "建议卖出价位（或区间）",
  "stop_loss": "建议止损价位",
  "add_point": "建议加仓价位（仅action=加仓时给出）",
  "risk_level": "低/中/高",
  "risk_factors": "当前持仓的主要风险因素（100字以内）"
}}
```"""

        logger.info(f"  分析 {code} {name}...")
        analysis = deepseek.analyze_for_json(system_prompt, user_prompt, temperature=AI_TEMPERATURE, max_tokens=2048)

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
    """将持仓分析结果格式化为可读报告（增强版）"""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  BBBIG 持仓分析报告（增强版）  {result['timestamp']}")
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

            # 盈亏颜色标记
            pnl_mark = "📈" if profit > 0 else "📉" if profit < 0 else "➖"
            lines.append(f"  {pnl_mark} 成本价: {cost}  |  当前价: {current}  |  盈亏: {profit:+.2f}%")
            lines.append(f"  趋势: {item.get('trend', '-')}")
            lines.append(f"  趋势分析: {item.get('trend_analysis', '-')}")
            lines.append(f"  量能分析: {item.get('volume_analysis', '-')}")
            if item.get('technical_summary'):
                lines.append(f"  技术面: {item['technical_summary']}")
            lines.append(f"  支撑位: {item.get('support_level', '-')}  |  压力位: {item.get('resistance_level', '-')}")

            # 操作建议高亮
            action = item.get('action', '-')
            action_emoji = {"持有": "🔵", "加仓": "🟢", "分批卖出": "🟡", "止损": "🔴"}.get(action, "⚪")
            lines.append(f"  {action_emoji} 操作建议: {action}")
            lines.append(f"    理由: {item.get('action_reason', '-')}")
            lines.append(f"  建议卖出点: {item.get('sell_point', '-')}")
            lines.append(f"  止损价位: {item.get('stop_loss', '-')}")
            if item.get('add_point'):
                lines.append(f"  加仓价位: {item['add_point']}")
            lines.append(f"  风险等级: {item.get('risk_level', '-')}")
            if item.get('risk_factors'):
                lines.append(f"  风险因素: {item['risk_factors']}")
        elif "analysis" in item:
            lines.append(f"  分析: {item['analysis']}")

    lines.append("\n⚠ 免责声明：以上分析仅供参考，不构成投资建议。投资有风险，入市需谨慎。")
    lines.append("=" * 70)
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    portfolio = load_portfolio()
    if not portfolio:
        print("暂无持仓，添加示例持仓...")
        add_holding("000001", 12.50, 1000, "平安银行")
        add_holding("600519", 1700.00, 100, "贵州茅台")

    result = run_portfolio_analysis()
    report = format_portfolio_report(result)
    print(report)
