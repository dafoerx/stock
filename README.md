# BBBIG — A股智能选股系统

基于 **DeepSeek 大模型 + 多因子量化模型** 的 A 股投资辅助工具。

数据源：Tushare Pro + SQLite 本地缓存

---

## 项目结构

```
BBBIG/
├── config.py                # 全局配置（API密钥/路径/选股参数/因子权重）
├── data_fetcher.py          # 数据获取层（Tushare Pro + 缓存调度）
├── db_cache.py              # SQLite 缓存层（日行情/指标/交易日历/概念）
├── stock_selector.py        # 核心选股引擎（多因子预筛 + DeepSeek AI评分）
├── backtester.py            # 回测验证模块（AI回测 + 纯量化回测）
├── deepseek_client.py       # DeepSeek API 封装
├── portfolio_analyzer.py    # 持仓分析模块（技术面评估/操作建议）
├── precache.py              # 数据预缓存脚本（批量同步历史数据）
├── scheduler.py             # 定时任务调度（每日自动执行）
├── main.py                  # 主入口（CLI）
├── backtest/
│   ├── __init__.py
│   ├── simulator.py         # 策略回测模拟器（真实资金管理/按周选股/止盈止损）
│   └── bbbig_backtest.py    # 三策略横向对比回测（RSI/MACD/SMA）
├── web/
│   ├── server.py            # Tornado Web 服务
│   ├── templates/           # 前端 HTML 模板
│   └── static/              # CSS / JS
├── data/
│   ├── stock_data.db        # SQLite 缓存数据库（本地，不提交）
│   ├── portfolio.json       # 持仓配置
│   └── results/             # 选股结果 JSON（本地，不提交）
├── logs/                    # 运行日志（本地，不提交）
└── README.md
```

---

## 各模块功能说明

### `config.py` — 全局配置
- Tushare / DeepSeek API 密钥（从环境变量读取，不硬编码）
- 选股核心参数：`TOP_N`、`KLINE_DAYS`、`MIN_MARKET_CAP`、`MIN_VOLUME`
- 多因子权重：`FACTOR_WEIGHTS`（趋势3.0/动量2.5/量能2.0/换手1.5/估值1.0/板块热度2.0/追高惩罚-1.5）
- 风控参数：`MAX_SAME_INDUSTRY`、`MARKET_RISK_THRESHOLD`

### `data_fetcher.py` — 数据获取层
- `fetch_all_stocks()` — 获取全量 A 股日行情（日行情 + 每日指标 + 基础信息三表合并）
- `fetch_stock_kline(code, days)` — 个股/指数日 K 线（缓存优先，增量补数据；指数走 `index_daily` 接口）
- `fetch_hot_sectors()` — 行业板块资金流向（降级为行业涨跌分组）
- `INDEX_CODES` — 指数白名单（`000300`/`000016`/`000905`/`000852`/`399001`/`399006`）
- `_is_index()` — 判断是否为指数代码，自动路由到正确 API

### `db_cache.py` — SQLite 缓存层
- 表：`stock_basic` / `daily` / `daily_basic` / `trade_cal` / `concept` / `concept_detail` / `sync_log`
- `get_missing_trade_dates()` — 计算需补数据的交易日
- `has_daily()` / `has_daily_basic()` — 缓存状态检查

### `stock_selector.py` — 核心选股引擎（6步流程）

| 步骤 | 说明 |
|------|------|
| 1 大盘风控 | 沪深300近5日涨跌幅 + MA20位置 → 低/中/高风险 |
| 2 板块资金流向 | 主力净流入 TOP 行业，后续加分 |
| 3 全量行情 | fetch_all_stocks() |
| 4 多因子预筛 | 200只 → K线 → 8因子评分 → TOP80 |
| 5 DeepSeek AI | 5维度×20分，返回JSON TOP10 |
| 6 回测重排序 | 回溯1~3周用真实K线验证，按胜率排序 |

**多因子评分权重：**

| 因子 | 权重 |
|------|------|
| 趋势（MA多头+MACD金叉） | 3.0 |
| 动量（近5日涨幅2~8%） | 2.5 |
| 量能（放量1.2~2.5x） | 2.0 |
| 板块热度 | 2.0 |
| 换手率（3%~10%） | 1.5 |
| 估值（PE 10~30, PB<3） | 1.0 |
| 追高惩罚（距20日高点<2%） | -1.5 |

### `backtester.py` — 单股回测验证（三种模式）
- **模式A** `run_backtest()` — 站在N周前视角选股，真实K线验证
- **模式B** `backtest_stock_list()` — 对今日推荐股回溯多周验证
- **模式C** `run_quant_backtest()` — 纯量化（不调AI），快速迭代

> ⚠️ 已知问题：模式B每轮让AI重新生成价格，非真实历史回测；`AI_MAX_TOKENS` 未定义（模式A会报 NameError）；胜率未扣除手续费（约0.4%双边）。

### `backtest/simulator.py` — 策略资金模拟回测 ✨新增

完整模拟真实资金管理流程：

```
初始资金（默认3万）
    ↓
每周运行完整选股（4周 = 4次AI选股）
    ↓
大盘风险 → 仓位上限（低80% / 中60% / 高30%）
    ↓
次日开盘：K线价格落入推荐买入区间 → 成交
（未落入 → 跳过，自动尝试下一只备选）
    ↓
逐日扫描：当日最高 ≥ 目标价 → 止盈卖出
         当日最低 ≤ 止损价 → 止损卖出
         同日双触  → 开盘方向判断先后
    ↓
手续费：买入0.03% + 卖出0.03% + 印花税0.1%（最低5元/笔）
止损冷静期：止损后10个交易日内不再买同一只股票
    ↓
输出：净值曲线 / 最终收益率 / 最大回撤 / 胜率 / 交易记录
```

**用法：**
```bash
# 默认 3万/4周
python3 -m BBBIG.main simulate

# 自定义
python3 -m BBBIG.main simulate 50000 6
```

> ⚠️ 已知限制：每周选股使用当前最新数据，不是真实历史时点数据（fetcher不支持历史回溯），第1周买入价格可能与历史实际价格偏差较大。

### `backtest/bbbig_backtest.py` — 三策略横向对比
- RSI策略 / MACD策略 / SMA策略三者同时回测同一批股票
- 对比三策略在90日窗口下各股的表现

### `deepseek_client.py` — AI接口封装
- `analyze_for_json()` — 调用 DeepSeek Chat API，强制JSON输出
- 自动重试3次，温度0.1（高确定性）

### `portfolio_analyzer.py` — 持仓分析
- 读取 `data/portfolio.json`，对每只持仓计算技术指标
- 输出：趋势分析/量能/支撑压力位/操作建议（卖出/止损/持有/加仓）

---

## 命令行用法

```bash
python3 -m BBBIG.main <command> [args]
```

| 命令 | 说明 | 示例 |
|------|------|------|
| `select` | 智能选股（TOP10） | `python3 -m BBBIG.main select` |
| `analyze` | 持仓分析 | `python3 -m BBBIG.main analyze` |
| `run` | 选股 + 持仓分析 | `python3 -m BBBIG.main run` |
| `add` | 添加持仓 | `python3 -m BBBIG.main add 000001 12.50 1000 平安银行` |
| `remove` | 移除持仓 | `python3 -m BBBIG.main remove 000001` |
| `list` | 持仓列表 | `python3 -m BBBIG.main list` |
| `backtest` | AI回测验证 | `python3 -m BBBIG.main backtest 1,2,3` |
| `qbacktest` | 纯量化回测 | `python3 -m BBBIG.main qbacktest 1,2,3,4` |
| `simulate` | 资金策略模拟 ✨ | `python3 -m BBBIG.main simulate 30000 4` |
| `web` | 启动Web界面 | `python3 -m BBBIG.main web 9999` |
| `serve` | 定时调度 | `python3 -m BBBIG.main serve` |

---

## 快速开始

```bash
# 1. 安装依赖
pip install pandas numpy tushare requests tornado schedule

# 2. 配置 API 密钥（不要提交 .env 文件！）
cp BBBIG/.env.example BBBIG/.env
# 编辑 .env，填入 TUSHARE_TOKEN 和 DEEPSEEK_API_KEY

# 3. 预缓存历史数据（首次运行）
python3 -m BBBIG.main precache

# 4. 执行今日选股
python3 -m BBBIG.main select

# 5. 策略回测模拟（验证过去4周效果）
python3 -m BBBIG.main simulate 30000 4
```

---

## 注意事项

- `.env` 已加入 `.gitignore`，**不会提交到 git**
- `data/stock_data.db`（26MB+）已加入 `.gitignore`，不提交
- `data/results/` 下的 JSON 结果不提交
- Tushare 免费账号有频率限制，批量K线获取时部分报错属正常（非致命，自动降级）
- 选股耗时约 6~10 分钟（受 DeepSeek API 响应速度影响）
- 策略模拟（simulate）每周调一次 DeepSeek，4周约耗时 30~50 分钟

---

## 免责声明

本系统仅供学习和研究使用，分析结果不构成任何投资建议。投资有风险，入市需谨慎。
