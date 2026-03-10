#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日定时调度器
每天定时执行选股和持仓分析任务
"""
import time
import logging
import schedule
from datetime import datetime

from BBBIG.config import DAILY_RUN_HOUR, DAILY_RUN_MINUTE, RESULT_DIR, LOG_DIR
from BBBIG.stock_selector import run_stock_selection, format_selection_report
from BBBIG.portfolio_analyzer import run_portfolio_analysis, format_portfolio_report

import os
import json

logger = logging.getLogger("BBBIG")


def _save_result(result: dict, prefix: str):
    """将分析结果保存到文件"""
    date_str = datetime.now().strftime("%Y%m%d")
    filepath = os.path.join(RESULT_DIR, f"{prefix}_{date_str}.json")
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"结果已保存至: {filepath}")
    except Exception as e:
        logger.error(f"保存结果异常: {e}")


def _is_trade_day() -> bool:
    """简单判断今天是否为交易日（周一至周五）"""
    today = datetime.now()
    return today.weekday() < 5  # 0=周一, 4=周五


def daily_job():
    """每日定时任务"""
    if not _is_trade_day():
        logger.info("今天不是交易日，跳过执行")
        return

    logger.info("=" * 70)
    logger.info("BBBIG 每日任务开始执行")
    logger.info("=" * 70)

    # 任务1：智能选股
    try:
        logger.info("\n>>> 任务1：智能选股 <<<")
        selection_result = run_stock_selection()
        report = format_selection_report(selection_result)
        print(report)
        _save_result(selection_result, "selection")
    except Exception as e:
        logger.error(f"选股任务异常: {e}", exc_info=True)

    # 任务2：持仓分析
    try:
        logger.info("\n>>> 任务2：持仓分析 <<<")
        portfolio_result = run_portfolio_analysis()
        report = format_portfolio_report(portfolio_result)
        print(report)
        _save_result(portfolio_result, "portfolio")
    except Exception as e:
        logger.error(f"持仓分析任务异常: {e}", exc_info=True)

    logger.info("BBBIG 每日任务执行完毕")


def start_scheduler():
    """启动定时调度器"""
    run_time = f"{DAILY_RUN_HOUR:02d}:{DAILY_RUN_MINUTE:02d}"
    logger.info(f"BBBIG 调度器启动，每日 {run_time} 执行分析任务")
    logger.info(f"结果保存目录: {RESULT_DIR}")
    logger.info(f"日志目录: {LOG_DIR}")

    schedule.every().day.at(run_time).do(daily_job)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    start_scheduler()
