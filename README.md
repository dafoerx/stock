# BBBIG - A股智能选股与持仓分析系统

基于 DeepSeek 大模型 + 多因子量化模型的A股投资辅助工具。

每日自动分析市场热点、K线趋势与技术指标，通过多因子评分预筛选 + AI深度分析，推荐最值得投资的股票；同时对持仓股进行技术面分析，给出操作建议。

## 核心特性

- **多因子预筛选**：趋势、动量、量能、换手率、估值、板块热度、追高惩罚 7维度评分
- **完整技术指标**：MA/MACD/KDJ/RSI/ATR/布林带/量价相关性
- **AI深度分析**：DeepSeek 大模型按 5 维度 × 20 分评分框架精选 TOP10
- **板块联动**：主力净流入 TOP 行业自动加分
- **大盘风控**：沪深300 趋势检测，高风险时减少推荐并优选防御标的
- **行业集中度控制**：同行业最多推荐 3 只，分散风险
- **回测验证**：AI 回测 + 纯量化回测，快速验证策略效果
- **持仓分析**：基于技术指标的卖出/止损/加仓建议
- **Web 可视化**：仪表盘 + 在线选股 + 持仓管理 + 历史记录

## 快速开始

### 1. 环境准备

```bash
# Python 3.8+
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install pandas numpy requests schedule tornado tushare
```

### 2. 配置

通过环境变量设置（推荐），或编辑 `BBBIG/config.py`：

```bash
export DEEPSEEK_API_KEY="your-api-key"
export TUSHARE_TOKEN="your-token"
```

### 3. 使用

```bash
# 智能选股（多因子预筛选 + AI分析 + 回测验证）
python -m BBBIG.main select

# 持仓管理
python -m BBBIG.main add 000001 12.50 1000 平安银行
python -m BBBIG.main list
python -m BBBIG.main analyze

# 启动 Web 界面（默认端口 9999）
python -m BBBIG.main web

# 每日定时调度（18:00 自动执行）
python -m BBBIG.main serve

# 回测
python -m BBBIG.main backtest 1,2,3     # AI回测
python -m BBBIG.main qbacktest 1,2,3,4  # 纯量化回测
```

## 命令一览

| 命令 | 说明 |
|------|------|
| `select` | 智能选股：多因子预筛选 → AI分析 → 回测验证 → 输出 TOP10 |
| `analyze` | 持仓分析：技术指标 + AI 给出操作建议 |
| `run` | 同时执行 select + analyze |
| `add <代码> <成本价> [股数] [名称]` | 添加持仓 |
| `remove <代码>` | 移除持仓 |
| `list` | 查看持仓列表 |
| `web [端口]` | 启动 Web 可视化界面（默认 9999） |
| `serve` | 启动每日定时调度器 |
| `backtest [1,2,3]` | AI 回测验证 |
| `qbacktest [1,2,3,4]` | 纯量化回测（不调 AI，快速迭代因子） |

## 选股流程

```
1. 大盘风控检查（沪深300 趋势 + MA20）
2. 获取行业/概念板块资金流向
3. 获取全量A股实时行情
4. 批量获取候选股K线 + 计算技术指标
5. 多因子评分预筛选 TOP80
6. DeepSeek AI 5维度评分精选 TOP10
7. 回测验证 + 按盈利概率排序
```

## 多因子评分体系

| 因子 | 权重 | 说明 |
|------|------|------|
| 趋势 | 3.0 | MA 多头排列 + MACD 信号 |
| 动量 | 2.5 | 近5日涨幅 2%~8% 最佳 |
| 量能 | 2.0 | 近3日温和放量 + 量价正相关 |
| 换手率 | 1.5 | 3%~10% 活跃度适中 |
| 估值 | 1.0 | PE 10~30 + PB < 3 |
| 板块热度 | 2.0 | 主力净流入 TOP 行业加分 |
| 追高惩罚 | -1.5 | 距20日高点过近减分 |

## 技术指标

选股和持仓分析均计算完整技术指标：

- **均线系统**：MA5/MA10/MA20 多空排列
- **MACD**：DIF/DEA/柱状图 + 金叉/死叉信号
- **KDJ**：K/D/J 值 + 超买超卖判断
- **RSI(14)**：强弱指数
- **布林带**：价格在上中下轨的位置
- **ATR**：平均真实波幅（波动率）
- **量价分析**：成交量趋势 + 量价相关性

## 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | 环境变量 | DeepSeek API Key |
| `TUSHARE_TOKEN` | 环境变量 | Tushare Pro Token |
| `TOP_N` | 10 | 推荐股票数量 |
| `KLINE_DAYS` | 30 | K线获取天数 |
| `MIN_MARKET_CAP` | 50亿 | 最小总市值 |
| `MIN_VOLUME` | 1亿 | 最小成交额 |
| `FACTOR_WEIGHTS` | 见 config.py | 多因子权重 |
| `MAX_SAME_INDUSTRY` | 3 | 同行业最多推荐数 |
| `MARKET_RISK_THRESHOLD` | -2.0% | 大盘风控阈值 |
| `AI_TEMPERATURE` | 0.1 | AI 分析温度 |
| `DAILY_RUN_HOUR` | 18 | 定时执行时间 |

## 目录结构

```
BBBIG/
├── config.py                # 配置（API Key、选股参数、多因子权重、风控参数）
├── data_fetcher.py          # 数据获取（东方财富行情 + K线 + 板块资金流向）
├── db_cache.py              # SQLite 缓存层（增量数据获取，减少 API 调用）
├── deepseek_client.py       # DeepSeek 大模型调用客户端
├── stock_selector.py        # 智能选股（多因子评分 + 技术指标 + 板块联动 + 风控）
├── portfolio_analyzer.py    # 持仓分析（技术指标增强 + AI 操作建议）
├── backtester.py            # 回测验证（AI回测 + 推荐股回测 + 纯量化回测）
├── scheduler.py             # 每日定时调度器
├── main.py                  # 命令行主入口
├── web/
│   ├── server.py            # Tornado Web 服务（端口 9999）
│   ├── templates/
│   │   └── index.html       # 前端页面
│   └── static/
│       ├── css/style.css
│       └── js/app.js
└── data/
    ├── portfolio.json       # 持仓配置
    ├── stock_data.db        # SQLite 缓存数据库
    └── results/             # 历史分析结果（JSON）
```

## Web API

| 路由 | 方法 | 功能 |
|------|------|------|
| `/` | GET | 首页 |
| `/api/selection/run` | POST | 触发选股 |
| `/api/selection/status` | GET | 选股状态 |
| `/api/selection/result` | GET | 选股结果 |
| `/api/portfolio/list` | GET | 持仓列表 |
| `/api/portfolio/add` | POST | 添加持仓 |
| `/api/portfolio/remove` | POST | 移除持仓 |
| `/api/portfolio/analyze` | POST | 触发持仓分析 |
| `/api/portfolio/analyze/status` | GET | 持仓分析状态 |
| `/api/portfolio/analyze/result` | GET | 持仓分析结果 |
| `/api/history/list` | GET | 历史记录列表 |
| `/api/history/detail` | GET | 历史记录详情 |

## 免责声明

本系统仅供学习和研究使用，分析结果不构成任何投资建议。投资有风险，入市需谨慎。
