#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BBBIG 系统配置文件
"""
import os
import json

# DeepSeek API 配置
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "***")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# Tushare Pro 配置
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "***")

# 数据目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
RESULT_DIR = os.path.join(DATA_DIR, "results")
DB_FILE = os.path.join(DATA_DIR, "stock_data.db")  # SQLite 缓存数据库

# 确保目录存在
for d in [DATA_DIR, LOG_DIR, RESULT_DIR]:
    os.makedirs(d, exist_ok=True)

# ========== 选股参数 ==========
TOP_N = 10  # 推荐股票数量
KLINE_DAYS = 30  # K线分析天数（获取30天数据）
MIN_MARKET_CAP = 50e8  # 最小市值 50亿
MIN_VOLUME = 1e8  # 最小成交额 1亿

# ========== 多因子评分权重 ==========
FACTOR_WEIGHTS = {
    "trend": 2.5,        # 趋势因子（MA多头排列）
    "momentum": 2.0,     # 动量因子（近5日涨幅适中）
    "volume": 2.0,       # 量能因子（近3日放量）
    "turnover": 1.5,     # 换手率因子
    "value": 2.0,        # 估值因子（PE/PB合理性）— 提高权重
    "sector_hot": 2.0,   # 板块热度因子（所属行业资金流入）
    "anti_chase": -2.5,  # 追高惩罚（距20日高点过近）— 加大惩罚
}

# ========== 交易成本 ==========
COMMISSION_RATE = 0.0003   # 佣金费率（买卖各万三）
STAMP_TAX_RATE = 0.001     # 印花税（卖出千一）
TOTAL_TRADE_COST = COMMISSION_RATE * 2 + STAMP_TAX_RATE  # 单次买卖总成本约 0.16%

# ========== 风控参数 ==========
MAX_SAME_INDUSTRY = 3       # 同行业最多推荐数量
INDEX_MA_DAYS = 20           # 大盘均线天数（用于判断牛熊）
MARKET_RISK_THRESHOLD = -2.0 # 大盘近5日跌幅超此值视为高风险（%）

# ========== AI 分析参数 ==========
AI_TEMPERATURE = 0.1   # AI温度（越低越确定性）
AI_MAX_TOKENS = 4096

# 每日执行时间（24小时制）
DAILY_RUN_HOUR = 18
DAILY_RUN_MINUTE = 0


def load_portfolio():
    """加载持仓配置"""
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def save_portfolio(portfolio):
    """保存持仓配置"""
    with open(PORTFOLIO_FILE, 'w', encoding='utf-8') as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)
