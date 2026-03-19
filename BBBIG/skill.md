# BBBIG A股智能选股系统 — Skill 定义

## 基本信息

- **名称**: BBBIG
- **描述**: A股智能选股与持仓分析系统，基于多因子量化模型 + DeepSeek AI 大模型
- **入口**: `python3 -m BBBIG.main <command>`（在 `/root/stock/` 目录执行）
- **数据源**: Tushare Pro + SQLite 本地缓存（`data/stock_data.db`）

---

## 能力列表

### 1. 智能选股 (select)

**触发**: 用户想了解今天买什么 / 推荐股票 / 选股

```bash
python3 -m BBBIG.main select
```

**流程**: 大盘风控 → 板块资金流向 → 全量A股行情 → K线技术指标 → 多因子预筛选 TOP80 → DeepSeek AI精选 TOP10 → 回测验证排序

**输出**: TOP10推荐股，含买入区间、目标价、止损价、仓位建议、评分明细

**耗时**: 约 6~10 分钟

---

### 2. 持仓分析 (analyze)

**触发**: 用户想分析手里的股票 / 该不该卖

```bash
python3 -m BBBIG.main analyze
```

**前提**: 需先用 `add` 命令添加持仓

**输出**: 每只持仓的趋势/量能/支撑压力位/操作建议（持有/卖出/止损/加仓）

---

### 3. 策略资金模拟回测 (simulate) ✨新增

**触发**: 用户想验证策略效果 / 模拟持仓回测 / 过去N周如果按系统操作能赚多少

```bash
python3 -m BBBIG.main simulate [初始资金] [回测周数]
```

**示例**:
```bash
python3 -m BBBIG.main simulate          # 默认 3万/4周
python3 -m BBBIG.main simulate 50000 6  # 5万/6周
```

**流程**:
1. 从N周前开始，每周重新运行完整选股
2. 按大盘风险分配仓位：低风险80% / 中风险60% / 高风险30%
3. 次日K线价格落入推荐买入区间才成交，否则跳过换下一只
4. 逐日检查：触达目标价止盈 / 触达止损价止损
5. 止损后10个交易日冷静期（不再买同股）
6. 手续费：买0.03% + 卖0.03% + 印花税0.1%

**输出**: 净值曲线 / 总收益率 / 最大回撤 / 胜率 / 完整交易记录

**耗时**: 约 30~50 分钟（每周一次DeepSeek选股）

**已知限制**: 历史时点选股使用当日最新数据，不是真实历史行情（第1周成交率可能偏低）

---

### 4. AI 回测 (backtest)

**触发**: 用户想验证选股策略准确性 / AI回测

```bash
python3 -m BBBIG.main backtest 1,2,3
```

参数为回测周数列表，往前推N周验证今日推荐股的历史胜率

**耗时**: 约 5~10 分钟（调用DeepSeek）

---

### 5. 纯量化回测 (qbacktest)

**触发**: 用户想快速验证因子效果 / 不调AI快速回测

```bash
python3 -m BBBIG.main qbacktest 1,2,3,4
```

不调AI，仅用多因子评分选股，秒级完成

---

### 6. 持仓管理

```bash
# 添加持仓
python3 -m BBBIG.main add <代码> <成本价> [股数] [名称]
python3 -m BBBIG.main add 000001 12.50 1000 平安银行

# 移除持仓
python3 -m BBBIG.main remove 000001

# 查看持仓列表
python3 -m BBBIG.main list
```

---

### 7. Web 界面 (web)

```bash
python3 -m BBBIG.main web [端口]   # 默认 9999
```

访问 http://localhost:9999，包含仪表盘/选股/持仓管理/历史记录

---

### 8. 定时调度 (serve)

```bash
python3 -m BBBIG.main serve
```

每天 18:00 自动执行选股 + 持仓分析（仅交易日）

---

## 环境要求

- Python 3.8+
- 依赖: `pandas numpy requests schedule tornado tushare`
- 环境变量（不要硬编码，使用 `.env` 文件）:
  - `TUSHARE_TOKEN` — Tushare Pro token
  - `DEEPSEEK_API_KEY` — DeepSeek API Key
  - `DEEPSEEK_BASE_URL` — 默认 `https://api.deepseek.com`
  - `DEEPSEEK_MODEL` — 默认 `deepseek-chat`

---

## 已知问题 / 技术债

| 编号 | 问题 | 严重度 | 状态 |
|------|------|--------|------|
| S1 | `AI_MAX_TOKENS` 未定义，backtester 模式A会 NameError | 🔴 高 | 待修 |
| S2 | `_get_trade_date_before()` 只跳周末，不处理法定节假日 | 🟡 中 | 待修 |
| S3 | `_evaluate_recommendation()` 未扣手续费，胜率虚高约0.4% | 🟡 中 | 待修 |
| S4 | simulator 历史选股用当日数据，非真实历史时点 | 🟡 中 | 已知限制 |
| S5 | concept/concept_detail 表为空（Tushare权限限制） | 🟢 低 | 非阻塞 |

---

## 输出文件

| 文件 | 说明 |
|------|------|
| `data/results/selection_YYYYMMDD_HHmmss.json` | 每次选股结果 |
| `data/results/backtest_ranked_YYYYMMDD_HHmmss.json` | 回测排序结果 |
| `data/results/simulation_YYYYMMDD_HHmmss.json` | 资金模拟回测报告 |
| `data/portfolio.json` | 当前持仓配置 |
| `logs/bbbig_YYYYMMDD.log` | 运行日志 |

---

## 免责声明

本系统仅供学习和研究使用，分析结果不构成任何投资建议。投资有风险，入市需谨慎。
