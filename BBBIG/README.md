# BBBIG - A股智能选股与持仓分析系统

基于 DeepSeek 大模型 + 多因子量化模型的A股投资辅助工具。

## 系统架构

```
全量A股行情
    ↓
[1] 大盘风控检查（沪深300 MA20 + 近5日涨跌）
    ↓
[2] 板块资金流向（行业+概念 TOP20）
    ↓
[3] 基础面硬性过滤（市值>50亿, 成交额>1亿, PE 0~200）
    ↓
[4] 批量获取K线 → 计算技术指标（MA/MACD/KDJ/RSI/ATR/布林带）
    ↓
[5] 多因子评分预筛选 TOP80
    │  趋势(3.0) + 动量(2.5) + 量能(2.0) + 换手率(1.5)
    │  + 估值(1.0) + 板块热度(2.0) + 追高惩罚(-1.5)
    ↓
[6] DeepSeek AI 5维度×20分评分 → TOP10
    │  趋势 + 量能 + 基本面 + 技术形态 + 板块热度
    │  同行业 ≤ 3只，高风险时优选防御标的
    ↓
[7] 回测验证（往前推1~3周，用真实K线验证盈亏）
    ↓
输出: 按盈利概率排序的推荐列表
```

## 模块说明

### stock_selector.py — 智能选股核心

- `calc_technical_indicators()` — 从K线计算全套技术指标
- `calc_ma/calc_ema/calc_rsi/calc_atr/calc_macd_signal/calc_kdj` — 单项指标计算
- `calc_bollinger_position()` — 布林带位置
- `_multifactor_score()` — 7维度多因子评分
- `_check_market_risk()` — 大盘风控（沪深300趋势检测）
- `_prefilter_candidates()` — 多因子预筛选
- `_apply_industry_limit()` — 行业集中度控制
- `run_stock_selection()` — 完整选股流程
- `format_selection_report()` — 报告格式化

### portfolio_analyzer.py — 持仓分析

- `add_holding/remove_holding/list_holdings` — 持仓 CRUD
- `run_portfolio_analysis()` — 完整持仓分析（技术指标 + AI）
- 输出: 趋势分析、量能分析、技术面判断、支撑/压力位、操作建议、止损价

### backtester.py — 回测验证

三种回测模式:
- **模式A: AI回测** (`run_backtest`) — 站在N周前用AI选股，真实K线验证
- **模式B: 推荐股回测** (`backtest_stock_list`) — 对已推荐股票多周验证
- **模式C: 纯量化回测** (`run_quant_backtest`) — 不调AI，纯因子评分选股验证

### data_fetcher.py — 数据获取

- 东方财富实时行情（全量A股）
- 东方财富个股K线
- 行业/概念板块资金流向
- SQLite 缓存层（`db_cache.py`）

### deepseek_client.py — AI客户端

- DeepSeek API 调用封装
- JSON 结构化输出解析

### config.py — 配置中心

所有可调参数集中管理: API Key、选股参数、多因子权重、风控参数、AI参数

## 命令行用法

```bash
python -m BBBIG.main <command> [args]
```

| 命令 | 说明 | 示例 |
|------|------|------|
| `select` | 智能选股 | `python -m BBBIG.main select` |
| `analyze` | 持仓分析 | `python -m BBBIG.main analyze` |
| `run` | 选股+持仓分析 | `python -m BBBIG.main run` |
| `add` | 添加持仓 | `python -m BBBIG.main add 000001 12.50 1000 平安银行` |
| `remove` | 移除持仓 | `python -m BBBIG.main remove 000001` |
| `list` | 持仓列表 | `python -m BBBIG.main list` |
| `web` | Web界面 | `python -m BBBIG.main web [端口]` |
| `serve` | 定时调度 | `python -m BBBIG.main serve` |
| `backtest` | AI回测 | `python -m BBBIG.main backtest 1,2,3` |
| `qbacktest` | 纯量化回测 | `python -m BBBIG.main qbacktest 1,2,3,4` |

## Web 界面

端口 9999，包含:
- **仪表盘**: 推荐股票数、持仓数、上次执行时间、市场热点
- **智能选股**: 一键触发选股分析，展示 TOP10 + 板块资金流向
- **持仓分析**: 在线管理持仓 + AI 操作建议
- **历史记录**: 过往选股/持仓分析结果

## 数据存储

- `data/portfolio.json` — 持仓配置
- `data/stock_data.db` — SQLite 缓存（增量获取，减少 API 调用）
- `data/results/` — 历史分析结果 JSON
- `logs/` — 运行日志

## 依赖

```
pandas, numpy, requests, schedule, tornado, tushare
```

## 免责声明

本系统仅供学习和研究使用，分析结果不构成任何投资建议。投资有风险，入市需谨慎。
