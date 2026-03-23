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
PROJECT_ROOT = os.path.dirname(BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
MARKET_CACHE_DIR = os.path.abspath(
    os.environ.get("BBBIG_MARKET_CACHE_DIR", os.path.join(PROJECT_ROOT, "market_data_cache"))
)
LOG_DIR = os.path.join(BASE_DIR, "logs")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
RESULT_DIR = os.path.join(DATA_DIR, "results")
LOCAL_DB_FILE = os.path.join(DATA_DIR, "stock_data.db")
SHARED_DB_FILE = os.path.join(MARKET_CACHE_DIR, "stock_data.db")


def resolve_db_file():
    """解析 SQLite 缓存数据库路径。"""
    explicit_db_file = os.environ.get("BBBIG_DB_FILE")
    if explicit_db_file:
        return os.path.abspath(explicit_db_file)
    if os.path.exists(SHARED_DB_FILE):
        return SHARED_DB_FILE
    return LOCAL_DB_FILE


DB_FILE = resolve_db_file()  # SQLite 缓存数据库

# 确保目录存在
for d in [DATA_DIR, LOG_DIR, RESULT_DIR, MARKET_CACHE_DIR, os.path.dirname(DB_FILE)]:
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
    "fundamental": 2.0,  # 基本面因子（ROE/营收增速/现金流）
}

# ========== 候选池分层抽样配置 ==========
# 按市值分层抽样，确保大中小盘均有代表
STRATIFIED_SAMPLING = {
    "large_cap": {"min_mv": 500e8, "count": 50},    # 大盘(>500亿): 50只
    "mid_cap": {"min_mv": 100e8, "max_mv": 500e8, "count": 80},  # 中盘(100~500亿): 80只
    "small_cap": {"min_mv": 50e8, "max_mv": 100e8, "count": 70},  # 小盘(50~100亿): 70只
}

# ========== 交易成本 ==========
COMMISSION_RATE = 0.0003   # 佣金费率（买卖各万三）
STAMP_TAX_RATE = 0.001     # 印花税（卖出千一）
TOTAL_TRADE_COST = COMMISSION_RATE * 2 + STAMP_TAX_RATE  # 单次买卖总成本约 0.16%

# ========== 风控参数 ==========
MAX_SAME_INDUSTRY = 2       # 同行业最多推荐数量（收紧：避免板块联动踩雷）
INDEX_MA_DAYS = 20           # 大盘均线天数（用于判断牛熊）
MARKET_RISK_THRESHOLD = -2.0 # 大盘近5日跌幅超此值视为高风险（%）
SIM_MAX_BUY_RANK = int(os.environ.get("SIM_MAX_BUY_RANK", "5"))  # 模拟器仅交易回测前N名
STOP_LOSS_MODE = os.environ.get("STOP_LOSS_MODE", "close_confirmed")  # close_confirmed / intraday

# ========== AI 分析参数 ==========
AI_TEMPERATURE = 0.1   # AI温度（越低越确定性）
AI_MAX_TOKENS = 4096

# ========== 消息面情绪分析（FinGPT 风格） ==========
SENTIMENT_ENABLED = True          # 是否启用情绪分析过滤（设为False可跳过此步骤）
SENTIMENT_NEGATIVE_THRESHOLD = -0.3  # 利空过滤阈值（情绪分数低于此值的行业/个股被过滤）
SENTIMENT_PENALTY_WEIGHT = 5.0    # 情绪降权系数（用于多因子评分中的情绪惩罚）
SENTIMENT_NEWS_DAYS = 3           # 新闻回溯天数（获取近N个交易日的新闻）
SENTIMENT_MAX_NEWS_PER_BATCH = 15 # 每个行业/个股最多分析的新闻条数

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
