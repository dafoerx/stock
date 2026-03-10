# BBBIG - A股智能选股与持仓分析系统

基于 DeepSeek V3.2 大模型的量化投资辅助工具。每日自动分析A股市场热点、K线趋势与成交量，推荐最值得投资的前10支股票；同时对持有股进行趋势分析，预测卖出点和止损点。

## 功能概览

| 功能 | 说明 |
|------|------|
| 智能选股 | 综合板块资金流向、K线形态、成交量变化、基本面指标，由大模型分析输出 TOP10 推荐股 |
| 持仓分析 | 对持有股进行近一周K线趋势分析，给出卖出点、止损点、操作建议 |
| Web 界面 | 可视化仪表盘，支持在线选股、持仓管理、AI分析、历史记录查看 |
| 每日调度 | 两项任务每天 18:00 自动执行（仅交易日） |

## 快速开始

### 1. 克隆代码

```bash
git clone <仓库地址>
cd stock
```

### 2. 安装 Python 环境

需要 Python 3.8+：

```bash
# 推荐使用虚拟环境
python3 -m venv venv
source venv/bin/activate
```

### 3. 安装依赖

```bash
pip install pandas requests schedule tornado
```

### 4. 启动 Web 服务

```bash
# 默认端口 9999
python -m BBBIG.main web

# 自定义端口
python -m BBBIG.main web 8080
```

启动后浏览器访问：http://localhost:9999

Web 界面包含 4 个页面：
- **仪表盘**：推荐股票数、持仓数、上次执行时间、市场热点概览
- **智能选股**：一键触发 DeepSeek 选股分析，展示 TOP10 推荐 + 板块资金流向
- **持仓分析**：在线管理持仓（增删）+ AI 分析卖出/止损点
- **历史记录**：查看过往选股/持仓分析的结果存档

### 5. 启动每日定时调度

后台运行，每天 18:00 自动执行选股和持仓分析（仅交易日）：

```bash
# 前台运行
python -m BBBIG.main serve

# 后台运行
nohup python -m BBBIG.main serve > /dev/null 2>&1 &
```

### 6. 同时启动 Web + 定时调度

```bash
# 终端1：启动 Web 服务
python -m BBBIG.main web

# 终端2：启动定时调度
python -m BBBIG.main serve
```

## 命令行用法

所有命令在项目根目录（`stock/`）下执行。

```bash
python -m BBBIG.main <command> [args]
```

| 命令 | 说明 | 示例 |
|------|------|------|
| `select` | 立即执行智能选股 | `python -m BBBIG.main select` |
| `analyze` | 立即执行持仓分析 | `python -m BBBIG.main analyze` |
| `run` | 同时执行选股 + 持仓分析 | `python -m BBBIG.main run` |
| `add` | 添加持仓 | `python -m BBBIG.main add 000001 12.50 1000 平安银行` |
| `remove` | 移除持仓 | `python -m BBBIG.main remove 000001` |
| `list` | 查看持仓列表 | `python -m BBBIG.main list` |
| `web` | 启动 Web 可视化界面 | `python -m BBBIG.main web [端口]` |
| `serve` | 启动每日定时调度器 | `python -m BBBIG.main serve` |

### 选股示例

```bash
python -m BBBIG.main select
```

选股流程：
1. 获取行业/概念板块资金流向，分析市场热点
2. 获取全量A股实时行情，按市值、成交额、市盈率、量比等预筛选
3. 批量获取候选股最近30天日K线数据
4. 将所有数据提交 DeepSeek 大模型综合分析
5. 输出 TOP10 推荐股票（含买入区间、目标价、止损价）

### 持仓管理示例

```bash
# 添加持仓：add <代码> <成本价> [股数] [名称]
python -m BBBIG.main add 000001 12.50 1000 平安银行
python -m BBBIG.main add 600519 1700.00 100 贵州茅台

# 查看持仓
python -m BBBIG.main list

# 移除持仓
python -m BBBIG.main remove 000001

# 分析持仓（给出卖出点、止损点、操作建议）
python -m BBBIG.main analyze
```

## 目录结构

```
BBBIG/
├── config.py                # 配置（API Key、选股参数等）
├── data_fetcher.py          # 数据获取（东方财富行情 + K线 + 板块资金流向）
├── deepseek_client.py       # DeepSeek 大模型调用客户端
├── stock_selector.py        # 智能选股核心逻辑
├── portfolio_analyzer.py    # 持仓分析核心逻辑
├── scheduler.py             # 每日定时调度器
├── main.py                  # 命令行主入口
├── web/
│   ├── server.py            # Tornado Web 服务（端口 9999）
│   ├── templates/
│   │   └── index.html       # 前端页面（SPA 单页应用）
│   └── static/
│       ├── css/style.css    # 样式
│       └── js/app.js        # 前端逻辑
└── data/
    ├── portfolio.json       # 持仓配置
    └── results/             # 历史分析结果（JSON）
```

## Web API 接口

| 路由 | 方法 | 功能 |
|------|------|------|
| `/` | GET | 首页 |
| `/api/selection/run` | POST | 触发选股任务（异步） |
| `/api/selection/status` | GET | 查询选股任务状态 |
| `/api/selection/result` | GET | 获取选股结果 |
| `/api/portfolio/list` | GET | 获取持仓列表 |
| `/api/portfolio/add` | POST | 添加持仓 |
| `/api/portfolio/remove` | POST | 移除持仓 |
| `/api/portfolio/analyze` | POST | 触发持仓分析（异步） |
| `/api/portfolio/analyze/status` | GET | 查询持仓分析状态 |
| `/api/portfolio/analyze/result` | GET | 获取持仓分析结果 |
| `/api/history/list` | GET | 历史记录列表 |
| `/api/history/detail` | GET | 历史记录详情 |

## 配置说明

编辑 `BBBIG/config.py` 可修改以下参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_KEY` | `sk-0aaf...` | DeepSeek API Key，也可通过环境变量 `DEEPSEEK_API_KEY` 覆盖 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 使用的模型，也可通过环境变量 `DEEPSEEK_MODEL` 覆盖 |
| `TOP_N` | `10` | 推荐股票数量 |
| `KLINE_DAYS` | `30` | K线数据获取天数 |
| `MIN_MARKET_CAP` | `50亿` | 候选股最小总市值 |
| `MIN_VOLUME` | `1亿` | 候选股最小成交额 |
| `DAILY_RUN_HOUR` | `18` | 每日执行时间（小时） |
| `DAILY_RUN_MINUTE` | `0` | 每日执行时间（分钟） |

## 输出结果

- 分析结果会打印到终端，同时保存为 JSON 文件到 `BBBIG/data/results/` 目录
- 日志文件保存在 `BBBIG/logs/` 目录

## 免责声明

本系统仅供学习和研究使用，分析结果不构成任何投资建议。投资有风险，入市需谨慎。
