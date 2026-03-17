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

# 确保目录存在
for d in [DATA_DIR, LOG_DIR, RESULT_DIR]:
    os.makedirs(d, exist_ok=True)

# 选股参数
TOP_N = 10  # 推荐股票数量
KLINE_DAYS = 30  # K线分析天数（获取30天数据，重点分析最近一周）
MIN_MARKET_CAP = 50e8  # 最小市值 50亿
MIN_VOLUME = 1e8  # 最小成交额 1亿

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
