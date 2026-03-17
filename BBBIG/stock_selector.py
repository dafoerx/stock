#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能选股模块（优化版）
多因子预筛选 + 技术指标计算 + 板块联动 + 行业集中度控制 + 大盘风控 + AI评分框架
"""
import logging
import time
import random
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from BBBIG.data_fetcher import fetcher
from BBBIG.deepseek_client import deepseek
from BBBIG.config import (
    TOP_N, KLINE_DAYS, MIN_MARKET_CAP, MIN_VOLUME,
    FACTOR_WEIGHTS, MAX_SAME_INDUSTRY, MARKET_RISK_THRESHOLD,
    AI_TEMPERATURE, AI_MAX_TOKENS
)

logger = logging.getLogger("BBBIG")


# ========== 技术指标计算 ==========

def calc_ma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=1).mean()


def calc_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta.clip(upper=0))
    avg_gain = gain.rolling(window=period, min_periods=period).mean().iloc[-1]
    avg_loss = loss.rolling(window=period, min_periods=period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean().iloc[-1]


def calc_bollinger_position(close_series: pd.Series, window: int = 20) -> float:
    """计算当前价格在布林带中的位置 (0=下轨, 0.5=中轨, 1=上轨)"""
    if len(close_series) < window:
        return 0.5
    ma = close_series.rolling(window).mean().iloc[-1]
    std = close_series.rolling(window).std().iloc[-1]
    if std == 0:
        return 0.5
    upper = ma + 2 * std
    lower = ma - 2 * std
    current = close_series.iloc[-1]
    if upper == lower:
        return 0.5
    return round((current - lower) / (upper - lower), 3)


def calc_macd_signal(close_series: pd.Series) -> dict:
    """计算MACD信号"""
    ema12 = calc_ema(close_series, 12)
    ema26 = calc_ema(close_series, 26)
    dif = ema12 - ema26
    dea = calc_ema(dif, 9)
    macd_hist = 2 * (dif - dea)

    result = {
        "dif": round(dif.iloc[-1], 4),
        "dea": round(dea.iloc[-1], 4),
        "macd": round(macd_hist.iloc[-1], 4),
    }
    # 金叉/死叉判断
    if len(dif) >= 2 and len(dea) >= 2:
        prev_diff = dif.iloc[-2] - dea.iloc[-2]
        curr_diff = dif.iloc[-1] - dea.iloc[-1]
        if prev_diff <= 0 and curr_diff > 0:
            result["signal"] = "金叉"
        elif prev_diff >= 0 and curr_diff < 0:
            result["signal"] = "死叉"
        elif curr_diff > 0:
            result["signal"] = "多头"
        else:
            result["signal"] = "空头"
    else:
        result["signal"] = "未知"
    return result


def calc_kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9) -> dict:
    """计算KDJ指标"""
    if len(close) < n:
        return {"k": 50, "d": 50, "j": 50, "signal": "未知"}
    low_n = low.rolling(window=n, min_periods=1).min()
    high_n = high.rolling(window=n, min_periods=1).max()
    rsv = ((close - low_n) / (high_n - low_n).replace(0, float('nan')) * 100).fillna(50)
    k = rsv.ewm(alpha=1/3, adjust=False).mean()
    d = k.ewm(alpha=1/3, adjust=False).mean()
    j = 3 * k - 2 * d
    result = {
        "k": round(k.iloc[-1], 2),
        "d": round(d.iloc[-1], 2),
        "j": round(j.iloc[-1], 2),
    }
    if result["k"] > result["d"] and result["j"] > 80:
        result["signal"] = "超买"
    elif result["k"] < result["d"] and result["j"] < 20:
        result["signal"] = "超卖"
    elif result["k"] > result["d"]:
        result["signal"] = "看多"
    else:
        result["signal"] = "看空"
    return result


def calc_technical_indicators(kline_df: pd.DataFrame) -> dict:
    """从K线DataFrame计算全套技术指标"""
    if kline_df.empty or len(kline_df) < 5:
        return {}
    close = kline_df['收盘']
    high = kline_df['最高']
    low = kline_df['最低']
    vol = kline_df['成交量']

    indicators = {}

    # 均线
    indicators['ma5'] = round(calc_ma(close, 5).iloc[-1], 2)
    indicators['ma10'] = round(calc_ma(close, 10).iloc[-1], 2) if len(close) >= 10 else indicators['ma5']
    indicators['ma20'] = round(calc_ma(close, 20).iloc[-1], 2) if len(close) >= 20 else indicators['ma10']

    # 均线多头排列: MA5 > MA10 > MA20
    indicators['ma_bull'] = indicators['ma5'] > indicators['ma10'] > indicators['ma20']
    # 均线空头排列: MA5 < MA10 < MA20
    indicators['ma_bear'] = indicators['ma5'] < indicators['ma10'] < indicators['ma20']

    # MACD
    macd_info = calc_macd_signal(close)
    indicators.update({f"macd_{k}": v for k, v in macd_info.items()})

    # KDJ
    kdj_info = calc_kdj(high, low, close)
    indicators.update({f"kdj_{k}": v for k, v in kdj_info.items()})

    # RSI
    if len(close) >= 14:
        indicators['rsi'] = calc_rsi(close, 14)
    else:
        indicators['rsi'] = 50.0

    # ATR (波动率)
    indicators['atr'] = round(calc_atr(high, low, close, min(14, len(close))), 4)
    indicators['atr_pct'] = round(indicators['atr'] / close.iloc[-1] * 100, 2) if close.iloc[-1] > 0 else 0

    # 布林带位置
    indicators['boll_pos'] = calc_bollinger_position(close)

    # 近5日涨幅
    if len(close) >= 5:
        indicators['chg_5d'] = round((close.iloc[-1] / close.iloc[-5] - 1) * 100, 2)
    else:
        indicators['chg_5d'] = 0.0

    # 近10日涨幅
    if len(close) >= 10:
        indicators['chg_10d'] = round((close.iloc[-1] / close.iloc[-10] - 1) * 100, 2)
    else:
        indicators['chg_10d'] = indicators['chg_5d']

    # 近20日最高价距离
    if len(high) >= 20:
        high_20d = high.tail(20).max()
        indicators['dist_high_20d'] = round((close.iloc[-1] / high_20d - 1) * 100, 2)
    else:
        indicators['dist_high_20d'] = 0.0

    # 成交量趋势: 近3日均量 / 前3日均量
    if len(vol) >= 6:
        recent_vol = vol.tail(3).mean()
        prev_vol = vol.iloc[-6:-3].mean()
        indicators['vol_trend'] = round(recent_vol / prev_vol, 2) if prev_vol > 0 else 1.0
    else:
        indicators['vol_trend'] = 1.0

    # 量价配合度: 涨时放量, 跌时缩量为正
    if len(close) >= 5 and len(vol) >= 5:
        chg = close.pct_change().tail(5)
        vol_chg = vol.pct_change().tail(5)
        corr = chg.corr(vol_chg)
        indicators['vol_price_corr'] = round(corr, 3) if not np.isnan(corr) else 0.0
    else:
        indicators['vol_price_corr'] = 0.0

    return indicators


# ========== 多因子评分 ==========

def _multifactor_score(row: pd.Series, indicators: dict, hot_industries: set) -> float:
    """
    多因子综合评分
    返回评分越高越好
    """
    weights = FACTOR_WEIGHTS
    score = 0.0

    # 1. 趋势因子: MA多头排列 +10, 空头 -5, MACD金叉 +5
    trend_score = 0
    if indicators.get('ma_bull'):
        trend_score += 10
    elif indicators.get('ma_bear'):
        trend_score -= 5
    if indicators.get('macd_signal') == '金叉':
        trend_score += 5
    elif indicators.get('macd_signal') == '多头':
        trend_score += 2
    elif indicators.get('macd_signal') == '死叉':
        trend_score -= 3
    score += trend_score * weights.get("trend", 3.0)

    # 2. 动量因子: 近5日涨幅2%~8%最佳, 超过10%减分
    chg_5d = indicators.get('chg_5d', 0)
    if 2 <= chg_5d <= 8:
        momentum_score = 8
    elif 0 <= chg_5d < 2:
        momentum_score = 4
    elif 8 < chg_5d <= 12:
        momentum_score = 3
    elif chg_5d > 12:
        momentum_score = -2  # 短期涨幅过大
    elif -3 <= chg_5d < 0:
        momentum_score = 2  # 小幅回调
    else:
        momentum_score = -3  # 大跌
    score += momentum_score * weights.get("momentum", 2.5)

    # 3. 量能因子: 近3日放量(1.2~2.5倍)加分, 极端放量减分
    vol_trend = indicators.get('vol_trend', 1.0)
    if 1.2 <= vol_trend <= 2.5:
        vol_score = 8
    elif 1.0 <= vol_trend < 1.2:
        vol_score = 3
    elif vol_trend > 2.5:
        vol_score = 1  # 极端放量需警惕
    else:
        vol_score = -2  # 缩量
    # 量价配合加分
    vp_corr = indicators.get('vol_price_corr', 0)
    if vp_corr > 0.3:
        vol_score += 3  # 量价正相关（涨时放量）
    score += vol_score * weights.get("volume", 2.0)

    # 4. 换手率因子: 3%~10%最佳
    turnover = row.get('换手率', 0)
    if 3 <= turnover <= 10:
        turnover_score = 8
    elif 1 <= turnover < 3:
        turnover_score = 4
    elif 10 < turnover <= 20:
        turnover_score = 3
    elif turnover > 20:
        turnover_score = -2
    else:
        turnover_score = 0
    score += turnover_score * weights.get("turnover", 1.5)

    # 5. 估值因子: PE 10~50 且 PB < 8 为合理
    pe = row.get('市盈率动', 0)
    pb = row.get('市净率', 0)
    value_score = 0
    if 10 <= pe <= 30:
        value_score += 5
    elif 30 < pe <= 50:
        value_score += 2
    elif pe > 80:
        value_score -= 3
    if 0 < pb < 3:
        value_score += 3
    elif 3 <= pb < 8:
        value_score += 1
    elif pb >= 8:
        value_score -= 2
    score += value_score * weights.get("value", 1.0)

    # 6. 板块热度因子: 属于主力净流入TOP行业加分
    industry = row.get('所处行业', '')
    if industry in hot_industries:
        score += 8 * weights.get("sector_hot", 2.0)

    # 7. 追高惩罚: 距20日最高点越近越减分
    dist_high = indicators.get('dist_high_20d', 0)  # 负值表示低于最高点
    if dist_high > -2:  # 距最高点不到2%
        chase_penalty = 8
    elif dist_high > -5:
        chase_penalty = 4
    elif dist_high > -10:
        chase_penalty = 1
    else:
        chase_penalty = 0
    score += chase_penalty * weights.get("anti_chase", -1.5)

    # 8. KDJ / RSI 辅助
    rsi = indicators.get('rsi', 50)
    if 30 <= rsi <= 60:
        score += 3  # 中性偏强
    elif rsi > 75:
        score -= 5  # 超买
    elif rsi < 25:
        score += 1  # 超卖可能反弹
    kdj_signal = indicators.get('kdj_signal', '')
    if kdj_signal == '超卖':
        score += 3
    elif kdj_signal == '超买':
        score -= 3

    return round(score, 2)


# ========== 大盘风控 ==========

def _check_market_risk() -> dict:
    """
    检查大盘风险状态
    返回: {"risk_level": "低/中/高", "index_chg_5d": float, "index_above_ma20": bool, "warning": str}
    """
    result = {"risk_level": "低", "index_chg_5d": 0.0, "index_above_ma20": True, "warning": ""}

    try:
        # 获取上证指数K线 (000001.SH → 用 399001 深证成指 or 000300 沪深300)
        index_kline = fetcher.fetch_stock_kline("000300", days=30)
        if index_kline.empty or len(index_kline) < 5:
            result["warning"] = "无法获取大盘指数数据，跳过风控检查"
            return result

        close = index_kline['收盘']

        # 近5日涨跌幅
        chg_5d = (close.iloc[-1] / close.iloc[-5] - 1) * 100
        result["index_chg_5d"] = round(chg_5d, 2)

        # MA20 位置
        if len(close) >= 20:
            ma20 = close.rolling(20).mean().iloc[-1]
            result["index_above_ma20"] = close.iloc[-1] > ma20
        else:
            result["index_above_ma20"] = True

        # 风险等级判断
        if chg_5d < MARKET_RISK_THRESHOLD and not result["index_above_ma20"]:
            result["risk_level"] = "高"
            result["warning"] = f"大盘近5日跌{chg_5d:.1f}%且跌破MA20，市场高风险"
        elif chg_5d < MARKET_RISK_THRESHOLD or not result["index_above_ma20"]:
            result["risk_level"] = "中"
            result["warning"] = f"大盘近5日涨跌{chg_5d:+.1f}%，风险中等"
        else:
            result["risk_level"] = "低"

    except Exception as e:
        logger.warning(f"大盘风控检查异常: {e}")
        result["warning"] = f"大盘风控检查异常: {e}"

    return result


# ========== K线摘要（增强版） ==========

def _format_kline_summary_enhanced(kline_df: pd.DataFrame, indicators: dict) -> str:
    """将K线 + 技术指标压缩为文本摘要"""
    if kline_df.empty:
        return "无数据"
    lines = []

    # 最近5个交易日明细
    recent = kline_df.tail(5)
    for _, row in recent.iterrows():
        lines.append(
            f"{row['日期']}: 开{row['开盘']:.2f} 高{row['最高']:.2f} "
            f"低{row['最低']:.2f} 收{row['收盘']:.2f} "
            f"量{row['成交量']:.0f} 额{row['成交额']:.0f} "
            f"涨跌幅{row['涨跌幅']:.2f}% 换手{row['换手率']:.2f}%"
        )

    # 技术指标摘要
    if indicators:
        lines.append(f"--- 技术指标 ---")
        lines.append(f"MA5:{indicators.get('ma5',0):.2f} MA10:{indicators.get('ma10',0):.2f} MA20:{indicators.get('ma20',0):.2f}"
                     f" {'多头排列' if indicators.get('ma_bull') else '空头排列' if indicators.get('ma_bear') else '交叉'}")
        lines.append(f"MACD: DIF={indicators.get('macd_dif',0):.4f} DEA={indicators.get('macd_dea',0):.4f} "
                     f"信号={indicators.get('macd_signal','未知')}")
        lines.append(f"KDJ: K={indicators.get('kdj_k',50):.1f} D={indicators.get('kdj_d',50):.1f} "
                     f"J={indicators.get('kdj_j',50):.1f} 信号={indicators.get('kdj_signal','未知')}")
        lines.append(f"RSI(14):{indicators.get('rsi',50):.1f} 布林位置:{indicators.get('boll_pos',0.5):.2f}")
        lines.append(f"近5日涨幅:{indicators.get('chg_5d',0):.2f}% 近10日涨幅:{indicators.get('chg_10d',0):.2f}%")
        lines.append(f"成交量趋势:{indicators.get('vol_trend',1):.2f}倍 量价相关:{indicators.get('vol_price_corr',0):.2f}")
        lines.append(f"距20日高点:{indicators.get('dist_high_20d',0):.2f}% ATR波动率:{indicators.get('atr_pct',0):.2f}%")

    # 周涨跌幅
    if len(kline_df) >= 5:
        week_start = kline_df.iloc[-5]["收盘"]
        week_end = kline_df.iloc[-1]["收盘"]
        week_change = (week_end - week_start) / week_start * 100
        lines.append(f"近一周涨跌幅: {week_change:.2f}%")

    # 成交量周环比
    if len(kline_df) >= 10:
        avg_vol_prev = kline_df.iloc[-10:-5]["成交量"].mean()
        avg_vol_recent = kline_df.iloc[-5:]["成交量"].mean()
        if avg_vol_prev > 0:
            vol_ratio = avg_vol_recent / avg_vol_prev
            lines.append(f"近一周成交量相比上周变化: {vol_ratio:.2f}倍")

    return "\n".join(lines)


# ========== 预筛选（多因子版） ==========

def _prefilter_candidates(all_stocks: pd.DataFrame, kline_map: dict,
                          hot_industries: set, top_n: int = 80) -> pd.DataFrame:
    """
    多因子预筛选：基础面过滤 + 技术指标评分
    """
    df = all_stocks.copy()

    # 基础面硬性过滤
    df = df[df["总市值"] >= MIN_MARKET_CAP]
    df = df[df["成交额"] >= MIN_VOLUME]
    df = df[(df["市盈率动"] > 0) & (df["市盈率动"] < 200)]
    df = df[(df["涨跌幅"] > -7) & (df["涨跌幅"] < 9.5)]
    df = df[df["市净率"] > 0]

    if df.empty:
        return df

    df = df.copy()

    # 为每只股票计算多因子评分
    scores = []
    indicators_map = {}
    for idx, row in df.iterrows():
        code = row['代码']
        kline = kline_map.get(code, pd.DataFrame())
        if not kline.empty:
            ind = calc_technical_indicators(kline)
        else:
            ind = {}
        indicators_map[code] = ind
        score = _multifactor_score(row, ind, hot_industries)
        scores.append(score)

    df["factor_score"] = scores
    df = df.sort_values("factor_score", ascending=False).head(top_n)
    return df.reset_index(drop=True), indicators_map


def _fetch_kline_batch(codes: list, days: int = KLINE_DAYS) -> dict:
    """批量获取K线数据"""
    kline_map = {}

    def _fetch_one(code):
        time.sleep(random.uniform(0.05, 0.3))
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


# ========== 行业集中度控制 ==========

def _apply_industry_limit(recommendations: list, max_per_industry: int = MAX_SAME_INDUSTRY) -> list:
    """限制同行业推荐数量，超限的替换为分析中的其他行业候选"""
    industry_count = {}
    filtered = []
    for rec in recommendations:
        industry = rec.get("industry", "未知")
        cnt = industry_count.get(industry, 0)
        if cnt < max_per_industry:
            filtered.append(rec)
            industry_count[industry] = cnt + 1
    return filtered


# ========== 主流程 ==========

def run_stock_selection() -> dict:
    """
    执行智能选股流程（优化版）
    返回: {"timestamp": ..., "hot_sectors": ..., "recommendations": [...], "analysis": "...",
           "market_risk": {...}}
    """
    logger.info("=" * 60)
    logger.info("开始执行智能选股（优化版）...")
    result = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hot_sectors": "",
        "recommendations": [],
        "analysis": "",
        "market_risk": {}
    }

    # Step 1: 大盘风控检查
    logger.info("[1/6] 大盘风控检查...")
    market_risk = _check_market_risk()
    result["market_risk"] = market_risk
    if market_risk["risk_level"] == "高":
        logger.warning(f"⚠ {market_risk['warning']}")
        logger.warning("市场高风险，将减少推荐数量并提示风险")

    # Step 2: 获取行业和概念板块资金流向
    logger.info("[2/6] 获取板块资金流向...")
    hot_sectors = fetcher.fetch_hot_sectors()
    concept_sectors = fetcher.fetch_concept_sectors()

    # 提取主力净流入TOP行业（用于多因子评分）
    hot_industries = set()
    sector_text = "【行业板块资金流向 TOP20】\n"
    if not hot_sectors.empty:
        # 取净流入为正的行业
        positive_sectors = hot_sectors[hot_sectors['主力净流入'] > 0]
        hot_industries = set(positive_sectors['板块名称'].head(10).tolist())
        for _, row in hot_sectors.iterrows():
            sector_text += f"  {row['板块名称']}: 涨跌幅{row['涨跌幅']:.2f}%, 主力净流入{row['主力净流入']/1e8:.2f}亿\n"

    concept_text = "【概念板块资金流向 TOP20】\n"
    if not concept_sectors.empty:
        for _, row in concept_sectors.iterrows():
            concept_text += f"  {row['板块名称']}: 涨跌幅{row['涨跌幅']:.2f}%, 主力净流入{row['主力净流入']/1e8:.2f}亿\n"

    result["hot_sectors"] = sector_text + "\n" + concept_text

    # Step 3: 获取全量A股行情
    logger.info("[3/6] 获取A股实时行情...")
    all_stocks = fetcher.fetch_all_stocks()
    if all_stocks.empty:
        logger.error("获取A股行情失败")
        result["analysis"] = "错误：无法获取A股行情数据"
        return result
    logger.info(f"  共获取 {len(all_stocks)} 只A股")

    # Step 4: 先获取活跃股K线，再进行多因子预筛选
    logger.info("[4/6] 获取候选股K线 + 多因子预筛选...")

    # 先粗筛出200只活跃股获取K线
    rough_df = all_stocks.copy()
    rough_df = rough_df[rough_df["总市值"] >= MIN_MARKET_CAP]
    rough_df = rough_df[rough_df["成交额"] >= MIN_VOLUME]
    rough_df = rough_df[(rough_df["市盈率动"] > 0) & (rough_df["市盈率动"] < 200)]
    rough_df = rough_df[rough_df["市净率"] > 0]

    # 按成交额排序取前200
    rough_df = rough_df.sort_values("成交额", ascending=False).head(200)
    rough_codes = rough_df["代码"].tolist()

    kline_map = _fetch_kline_batch(rough_codes, days=KLINE_DAYS)
    logger.info(f"  成功获取 {len(kline_map)} 只股票K线")

    # 多因子预筛选
    prefilter_result = _prefilter_candidates(all_stocks, kline_map, hot_industries, top_n=80)
    if isinstance(prefilter_result, tuple):
        candidates, indicators_map = prefilter_result
    else:
        candidates = prefilter_result
        indicators_map = {}

    if candidates.empty:
        logger.error("预筛选后无候选股票")
        result["analysis"] = "错误：预筛选后无候选股票"
        return result
    logger.info(f"  多因子预筛选后 {len(candidates)} 只候选股票")

    # 为还没有K线的候选股补充获取
    missing_codes = [c for c in candidates["代码"].tolist() if c not in kline_map]
    if missing_codes:
        extra_klines = _fetch_kline_batch(missing_codes, days=KLINE_DAYS)
        kline_map.update(extra_klines)

    # Step 5: 构造增强版大模型 prompt
    logger.info("[5/6] 调用 DeepSeek 大模型进行综合分析...")

    # 根据风险等级调整推荐数量
    effective_top_n = TOP_N
    risk_note = ""
    if market_risk["risk_level"] == "高":
        effective_top_n = max(3, TOP_N // 2)
        risk_note = f"\n⚠ 当前市场风险较高（大盘近5日跌{market_risk['index_chg_5d']:.1f}%且跌破MA20），请优先选择防御性标的，减少推荐数量至{effective_top_n}只。"
    elif market_risk["risk_level"] == "中":
        risk_note = f"\n⚠ 当前市场风险中等（大盘近5日涨跌{market_risk['index_chg_5d']:+.1f}%），请适当控制仓位。"

    # 构造候选股数据摘要（含技术指标）
    stock_summaries = []
    for _, row in candidates.head(50).iterrows():
        code = row["代码"]
        kline = kline_map.get(code, pd.DataFrame())
        ind = indicators_map.get(code, {})
        if not ind and not kline.empty:
            ind = calc_technical_indicators(kline)
        kline_text = _format_kline_summary_enhanced(kline, ind)
        stock_summaries.append(
            f"【{code} {row['名称']}】 行业:{row['所处行业']} "
            f"最新价:{row['最新价']:.2f} 涨跌幅:{row['涨跌幅']:.2f}% "
            f"成交额:{row['成交额']/1e8:.2f}亿 换手率:{row['换手率']:.2f}% "
            f"市盈率:{row['市盈率动']:.1f} 市净率:{row['市净率']:.2f} "
            f"总市值:{row['总市值']/1e8:.0f}亿 量比:{row['量比']:.2f} "
            f"多因子评分:{row.get('factor_score', 0):.1f}\n"
            f"  K线及技术指标:\n  {kline_text}"
        )

    system_prompt = """你是一位资深的A股量化分析师和投资顾问。请基于提供的市场数据进行专业分析。

你必须严格按照以下评分框架对每只候选股票进行打分（满分100分）：

## 评分维度（共5项，每项20分）：

### 1. 趋势评分（20分）
- 均线多头排列（MA5>MA10>MA20）: +15分
- MACD金叉或多头: +5分
- 均线空头排列: -10分
- MACD死叉: -5分

### 2. 量能评分（20分）
- 近3日温和放量（1.2~2.5倍）且量价正相关: +15分
- 极端放量（>3倍）: +5分（可能冲顶）
- 缩量: +8分（如在上涨趋势中可能是洗盘）
- 量价背离（涨时缩量或跌时放量）: -5分

### 3. 基本面评分（20分）
- PE 10~30 且 PB < 3: +15分
- PE 30~50: +10分
- PE > 80 或 PB > 8: -5分
- 行业龙头/市值>500亿: +5分

### 4. 技术形态评分（20分）
- RSI 30~60（强势但不超买）: +10分
- KDJ 超卖区金叉: +10分
- 布林带下轨附近（<0.3）: +8分
- RSI > 75 或 KDJ超买: -10分

### 5. 板块热度评分（20分）
- 所属行业为当日主力净流入TOP5: +15分
- 所属概念板块为当日热点: +10分
- 所属行业主力净流出: -5分

## 输出要求：
- 选出总评分最高的股票
- 同一行业最多选3只
- 必须给出每只股票的总评分和各维度得分
- 高风险市场环境下优先选择低波动、高分红防御标的"""

    user_prompt = f"""请分析以下A股市场数据，从候选股票中选出最值得投资的前{effective_top_n}支股票。
{risk_note}
当前日期: {datetime.now().strftime('%Y-%m-%d')}

{result['hot_sectors']}

以下是经过多因子预筛选的候选股票（含完整技术指标）：

{''.join(stock_summaries[:40])}

请完成以下分析任务：
1. **市场热点分析**：基于板块资金流向+大盘环境，分析当前A股市场热点和趋势方向
2. **逐股评分**：按上述5维度评分框架对候选股打分
3. **选股推荐**：选出总评分最高的前{effective_top_n}支（同行业不超过{MAX_SAME_INDUSTRY}只）
4. **关键风险点**：每只股票指出1个最大风险因素
5. **潜在催化剂**：每只股票指出可能的上涨催化因素

请严格按以下JSON格式返回：
```json
{{
  "market_analysis": "对当前A股市场热点和趋势的分析（200字以内）",
  "market_risk_assessment": "对当前市场风险的评估（100字以内）",
  "recommendations": [
    {{
      "rank": 1,
      "code": "股票代码",
      "name": "股票名称",
      "industry": "所属行业",
      "current_price": 当前价格,
      "scores": {{
        "trend": 0,
        "volume": 0,
        "fundamental": 0,
        "technical": 0,
        "sector_heat": 0,
        "total": 0
      }},
      "reason": "推荐理由（含K线分析、量能分析、基本面分析，150字以内）",
      "risk_factor": "最大风险因素（50字以内）",
      "catalyst": "潜在催化剂（50字以内）",
      "suggested_buy_range": "建议买入区间（如 12.5-13.0）",
      "target_price": "短期目标价",
      "stop_loss": "止损价",
      "position_weight": "建议仓位比例（如 15%）"
    }}
  ]
}}
```"""

    analysis_result = deepseek.analyze_for_json(
        system_prompt, user_prompt,
        temperature=AI_TEMPERATURE, max_tokens=AI_MAX_TOKENS
    )

    if isinstance(analysis_result, dict):
        if "recommendations" in analysis_result:
            recs = analysis_result["recommendations"]
            # 应用行业集中度限制
            recs = _apply_industry_limit(recs, MAX_SAME_INDUSTRY)
            result["recommendations"] = recs
            result["analysis"] = analysis_result.get("market_analysis", "")
            result["market_risk_assessment"] = analysis_result.get("market_risk_assessment", "")
        elif "raw_response" in analysis_result:
            result["analysis"] = analysis_result["raw_response"]
        else:
            result["analysis"] = str(analysis_result)
    else:
        result["analysis"] = str(analysis_result)

    logger.info(f"选股完成，推荐 {len(result['recommendations'])} 只股票")

    # Step 6: 自动回测验证
    if result["recommendations"]:
        logger.info("[6/6] 对推荐股票进行回测验证...")
        from BBBIG.backtester import backtest_stock_list
        backtest_report = backtest_stock_list(result["recommendations"], weeks_list=[1, 2, 3])
        result["backtest_report"] = backtest_report
        if backtest_report.get("stock_results"):
            ranked_codes = [sr["code"] for sr in backtest_report["stock_results"]]
            rec_map = {r["code"]: r for r in result["recommendations"]}
            sorted_recs = []
            for i, code in enumerate(ranked_codes):
                if code in rec_map:
                    rec = rec_map[code]
                    rec["rank"] = i + 1
                    sr = next((s for s in backtest_report["stock_results"] if s["code"] == code), None)
                    if sr:
                        rec["backtest_win_rate"] = sr["win_rate"]
                        rec["backtest_avg_profit"] = sr["avg_profit_pct"]
                        rec["backtest_rounds"] = sr["rounds"]
                    sorted_recs.append(rec)
            result["recommendations"] = sorted_recs
            logger.info("推荐股票已按回测盈利概率重新排序")
    else:
        logger.info("[6/6] 无推荐股票，跳过回测")

    return result


def format_selection_report(result: dict) -> str:
    """将选股结果格式化为可读报告（增强版）"""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  BBBIG 智能选股报告（优化版）  {result['timestamp']}")
    lines.append("=" * 70)

    # 大盘风控信息
    market_risk = result.get("market_risk", {})
    if market_risk:
        risk_emoji = {"低": "🟢", "中": "🟡", "高": "🔴"}.get(market_risk.get("risk_level", "低"), "⚪")
        lines.append(f"\n{risk_emoji} 大盘风险等级: {market_risk.get('risk_level', '未知')}")
        if market_risk.get("warning"):
            lines.append(f"  {market_risk['warning']}")

    if result.get("hot_sectors"):
        lines.append("\n" + result["hot_sectors"])

    if result.get("analysis"):
        lines.append("\n【市场热点分析】")
        lines.append(result["analysis"])

    if result.get("market_risk_assessment"):
        lines.append("\n【市场风险评估】")
        lines.append(result["market_risk_assessment"])

    if result.get("recommendations"):
        has_backtest = any("backtest_win_rate" in r for r in result["recommendations"])
        has_scores = any("scores" in r for r in result["recommendations"])

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

            # 评分明细
            scores = rec.get("scores", {})
            if scores:
                lines.append(f"  评分: 趋势{scores.get('trend',0)} 量能{scores.get('volume',0)} "
                             f"基本面{scores.get('fundamental',0)} 技术{scores.get('technical',0)} "
                             f"板块{scores.get('sector_heat',0)} → 总分{scores.get('total',0)}")

            lines.append(f"  推荐理由: {rec.get('reason', '-')}")
            lines.append(f"  风险因素: {rec.get('risk_factor', '-')}")
            lines.append(f"  催化剂: {rec.get('catalyst', '-')}")
            lines.append(f"  建议买入区间: {rec.get('suggested_buy_range', '-')}")
            lines.append(f"  短期目标价: {rec.get('target_price', '-')}")
            lines.append(f"  止损价: {rec.get('stop_loss', '-')}")
            lines.append(f"  建议仓位: {rec.get('position_weight', '-')}")

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
