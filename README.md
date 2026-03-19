# BBBIG — A股智能选股系统

基于 **DeepSeek 大模型 + 多因子量化模型** 的 A 股投资辅助工具。

数据源：Tushare Pro + SQLite 本地缓存

---

## 项目结构

```
BBBIG/
├── config.py            # 全局配置（API密钥/路径/选股参数/因子权重）
├── data_fetcher.py      # 数据获取层（Tushare Pro + 缓存调度）
├── db_cache.py          # SQLite 缓存层（日行情/指标/交易日历/概念）
├── stock_selector.py    # 核心选股引擎（多因子预筛 + DeepSeek AI评分）
├── backtester.py        # 回测验证模块（AI回测 + 纯量化回测）
├── deepseek_client.py   # DeepSeek API 封装
├── portfolio_analyzer.py# 持仓分析模块（技术面评估/操作建议）
├── precache.py          # 数据预缓存脚本（批量同步历史数据）
├── scheduler.py         # 定时任务调度（每日自动执行）
├── main.py              # 主入口（CLI：select/analyze/backtest/precache）
├── web/
│   ├── server.py        # Tornado Web 服务
│   ├── templates/       # 前端 HTML 模板
│   └── static/          # CSS / JS
├── data/
│   ├── stock_data.db    # SQLite 缓存数据库（本地，不提交）
│   ├── portfolio.json   # 持仓配置
│   └── results/         # 选股结果 JSON（本地，不提交）
├── logs/                # 运行日志（本地，不提交）
└── README.md            # 模块说明文档
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
- `fetch_hot_sectors()` — 行业板块资金流向（`moneyflow_ind_ths` 同花顺接口，内存缓存，降级为行业涨跌分组）
- `fetch_concept_sectors()` — 概念板块资金流向（Tushare `concept` + `concept_detail` + `moneyflow`）
- 自动维护交易日历、增量同步日行情、每日指标

### `db_cache.py` — SQLite 缓存层
- 表：`stock_basic` / `daily` / `daily_basic` / `trade_cal` / `concept` / `concept_detail` / `sync_log`
- `get_missing_trade_dates()` — 计算需补数据的交易日
- `has_daily()` / `has_daily_basic()` — 缓存状态检查
- `is_stock_basic_fresh()` — 24小时新鲜度判断

### `stock_selector.py` — 核心选股引擎（883行）
**6步流程：**
1. **大盘风控** — 沪深300近30日K线，计算近5日涨跌幅 + MA20位置，判断低/中/高风险
2. **板块资金流向** — 获取主力净流入TOP行业，用于后续加分
3. **全量A股行情** — 调用 `fetch_all_stocks()`
4. **多因子预筛选** — 粗筛200只（TOP140成交额 + 随机60只）→ 批量获取K线 → 8因子评分 → 精选80只
5. **DeepSeek AI评分** — 前50只完整数据打包Prompt，AI按5维度×20分评分，返回JSON推荐
6. **回测重排序** — 对AI推荐的10只，回溯1/2/3周前验证，按胜率重新排序

**多因子评分（`_multifactor_score`）：**

| 因子 | 权重 | 说明 |
|------|------|------|
| 趋势 | 3.0 | MA多头排列+10，MACD金叉+5 |
| 动量 | 2.5 | 近5日涨2~8%最佳 |
| 量能 | 2.0 | 放量1.2~2.5x加分 |
| 换手率 | 1.5 | 3%~10%最佳 |
| 估值 | 1.0 | PE 10~30 + PB<3 加分 |
| 板块热度 | 2.0 | 所属行业在净流入TOP10加分 |
| 追高惩罚 | -1.5 | 距20日高点<2%扣分 |

### `backtester.py` — 回测验证（941行）
- `backtest_stock_list(stocks, weeks)` — 对推荐股票回溯N周前视角，调用DeepSeek模拟分析，对比实际价格判断胜负
- `run_pure_quantitative_backtest()` — 纯量化回测（不调AI，基于技术指标规则）
- 结果输出：买入价/目标价/止损价/最高最低/最终结果/收益率

### `deepseek_client.py` — AI接口封装
- `analyze_for_json(system_prompt, user_prompt)` — 调用 DeepSeek Chat API，强制返回 JSON 格式
- 自动重试（3次），温度 `AI_TEMPERATURE=0.1`（高确定性）

### `portfolio_analyzer.py` — 持仓分析
- 读取 `data/portfolio.json` 中的持仓
- 计算每只持仓的技术指标，给出 **卖出/止损/持有/加仓** 操作建议
- 输出持仓盈亏、风险评估

### `precache.py` — 数据预缓存
- 批量同步历史日行情、每日指标、交易日历
- 首次部署或数据补全时使用

### `scheduler.py` — 定时调度
- 每日收盘后（默认18:00）自动执行选股流程
- 也可配合系统 cron 使用

### `main.py` — 主入口（CLI）

```bash
# 执行今日选股
python3 -m BBBIG.main select

# 分析持仓
python3 -m BBBIG.main analyze

# 纯量化回测
python3 -m BBBIG.main backtest

# 预缓存历史数据
python3 -m BBBIG.main precache

# 启动 Web 服务
python3 -m BBBIG.main web
```

### `web/server.py` — Web 可视化服务
- 基于 Tornado，默认端口 8888
- 提供：仪表盘 / 在线选股 / 持仓管理 / 历史选股记录

---

## 快速开始

```bash
# 1. 安装依赖
pip install pandas numpy tushare requests

# 2. 配置 API 密钥（不要提交 .env 文件！）
cp BBBIG/.env.example BBBIG/.env
# 编辑 .env，填入 TUSHARE_TOKEN 和 DEEPSEEK_API_KEY

# 3. 预缓存历史数据（首次运行）
TUSHARE_TOKEN=xxx DEEPSEEK_API_KEY=xxx python3 -m BBBIG.main precache

# 4. 执行选股
TUSHARE_TOKEN=xxx DEEPSEEK_API_KEY=xxx python3 -m BBBIG.main select
```

---

## 注意事项

- `.env` 文件已加入 `.gitignore`，**不会提交到 git**
- `data/stock_data.db`（26MB+）已加入 `.gitignore`，不提交
- `data/results/` 下的 JSON 结果文件不提交
- Tushare 免费账号存在频率限制，批量 K 线获取时会有部分报错（非致命，会从缓存降级）
