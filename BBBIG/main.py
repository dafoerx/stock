#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BBBIG 主入口
支持命令行操作：
  python -m BBBIG.main select          # 立即执行选股
  python -m BBBIG.main analyze         # 立即执行持仓分析
  python -m BBBIG.main run             # 立即执行选股+持仓分析
  python -m BBBIG.main add <代码> <成本价> [股数] [名称]   # 添加持仓
  python -m BBBIG.main remove <代码>   # 移除持仓
  python -m BBBIG.main list            # 查看持仓列表
  python -m BBBIG.main serve           # 启动每日定时调度器
  python -m BBBIG.main web [端口]      # 启动 Web 可视化界面（默认端口 9999）
  python -m BBBIG.main backtest [1,2,3]  # AI回测验证（往前推N周选股并验证盈亏）
  python -m BBBIG.main qbacktest [1,2,3,4]  # 纯量化回测（不调AI，快速验证因子效果）
"""
import sys
import os
import logging
from datetime import datetime

# 将项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from BBBIG.config import LOG_DIR, RESULT_DIR
from BBBIG.stock_selector import run_stock_selection, format_selection_report
from BBBIG.portfolio_analyzer import (
    run_portfolio_analysis, format_portfolio_report,
    add_holding, remove_holding, list_holdings
)
from BBBIG.scheduler import daily_job, start_scheduler

import json


def setup_logging():
    """配置日志"""
    log_file = os.path.join(LOG_DIR, f"bbbig_{datetime.now().strftime('%Y%m%d')}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding='utf-8')
        ]
    )


class _SafeEncoder(json.JSONEncoder):
    """兼容 numpy/pandas bool_、int64、float64 等类型的 JSON 编码器"""
    def default(self, obj):
        import numpy as np
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


def save_result(result: dict, prefix: str):
    """保存结果到JSON文件"""
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(RESULT_DIR, f"{prefix}_{date_str}.json")
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2, cls=_SafeEncoder)
    print(f"\n结果已保存至: {filepath}")


def cmd_select():
    """执行选股"""
    print("开始执行智能选股...\n")
    result = run_stock_selection()
    report = format_selection_report(result)
    print(report)
    save_result(result, "selection")


def cmd_analyze():
    """执行持仓分析"""
    print("开始执行持仓分析...\n")
    result = run_portfolio_analysis()
    report = format_portfolio_report(result)
    print(report)
    save_result(result, "portfolio")


def cmd_run():
    """执行选股+持仓分析"""
    cmd_select()
    print("\n")
    cmd_analyze()


def cmd_add(args):
    """添加持仓"""
    if len(args) < 2:
        print("用法: python -m BBBIG.main add <代码> <成本价> [股数] [名称]")
        print("示例: python -m BBBIG.main add 000001 12.50 1000 平安银行")
        return
    code = args[0]
    cost = float(args[1])
    shares = int(args[2]) if len(args) > 2 else 0
    name = args[3] if len(args) > 3 else ""
    add_holding(code, cost, shares, name)
    print(f"已添加持仓: {code} 成本价={cost} 股数={shares}")
    _show_holdings()


def cmd_remove(args):
    """移除持仓"""
    if len(args) < 1:
        print("用法: python -m BBBIG.main remove <代码>")
        return
    code = args[0]
    remove_holding(code)
    print(f"已移除持仓: {code}")
    _show_holdings()


def _show_holdings():
    """显示持仓列表"""
    holdings = list_holdings()
    if not holdings:
        print("\n当前无持仓股票")
        return
    print(f"\n当前持仓 ({len(holdings)} 只):")
    print("-" * 60)
    print(f"  {'代码':<10} {'名称':<10} {'成本价':<10} {'股数':<10} {'添加时间'}")
    print("-" * 60)
    for h in holdings:
        print(f"  {h['code']:<10} {h.get('name', '-'):<10} {h['cost']:<10.2f} "
              f"{h.get('shares', 0):<10} {h.get('add_time', '-')}")
    print("-" * 60)


def cmd_list():
    """查看持仓列表"""
    _show_holdings()


def cmd_serve():
    """启动定时调度器"""
    print("启动 BBBIG 每日定时调度器...")
    start_scheduler()


def cmd_web(args):
    """启动 Web 可视化界面"""
    port = int(args[0]) if args else 9999
    from BBBIG.web.server import start_web
    start_web(port=port)


def cmd_backtest(args):
    """回测验证选股准确率"""
    from BBBIG.backtester import run_backtest, format_backtest_report
    weeks_list = [1, 2, 3]
    if args:
        try:
            weeks_list = [int(x) for x in args[0].split(",")]
        except ValueError:
            print("用法: python -m BBBIG.main backtest [1,2,3]")
            print("示例: python -m BBBIG.main backtest 1,2,3")
            return
    print(f"开始回测验证（往前推 {weeks_list} 周）...\n")
    report = run_backtest(weeks_list)
    print(format_backtest_report(report))


def print_usage():
    """打印使用说明"""
    print("""
BBBIG - A股智能选股与持仓分析系统
基于 DeepSeek 大模型的量化投资辅助工具

用法:
  python -m BBBIG.main <command> [args]

命令:
  select              立即执行智能选股（推荐TOP10）
  analyze             立即执行持仓分析
  run                 立即执行选股 + 持仓分析
  add <代码> <成本价> [股数] [名称]   添加持仓
  remove <代码>       移除持仓
  list                查看持仓列表
  serve               启动每日定时调度器（每天18:00自动执行）
  web [端口]           启动 Web 可视化界面（默认端口 9999）
  backtest [1,2,3]    AI回测验证（往前推N周选股并用真实K线验证盈亏）
  qbacktest [1,2,3,4] 纯量化回测（不调AI，快速验证多因子评分效果）

示例:
  python -m BBBIG.main select
  python -m BBBIG.main add 000001 12.50 1000 平安银行
  python -m BBBIG.main add 600519 1700.00 100 贵州茅台
  python -m BBBIG.main analyze
  python -m BBBIG.main web
  python -m BBBIG.main backtest
  python -m BBBIG.main backtest 1,2
  python -m BBBIG.main serve
""")


def main():
    setup_logging()

    if len(sys.argv) < 2:
        print_usage()
        return

    command = sys.argv[1].lower()
    args = sys.argv[2:]

    commands = {
        "select": lambda: cmd_select(),
        "analyze": lambda: cmd_analyze(),
        "run": lambda: cmd_run(),
        "add": lambda: cmd_add(args),
        "remove": lambda: cmd_remove(args),
        "list": lambda: cmd_list(),
        "serve": lambda: cmd_serve(),
        "web": lambda: cmd_web(args),
        "backtest": lambda: cmd_backtest(args),
        "qbacktest": lambda: cmd_qbacktest(args),
    }

    if command in commands:
        commands[command]()
    else:
        print(f"未知命令: {command}")
        print_usage()


if __name__ == "__main__":
    main()
