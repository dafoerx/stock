# BBBIG A股智能选股系统 — Skill 定义

## 基本信息

- **名称**: BBBIG
- **描述**: A股智能选股与持仓分析系统，基于多因子量化模型 + DeepSeek AI 大模型
- **入口**: `python -m BBBIG.main <command>`（在项目根目录执行）

## 能力列表

### 1. 智能选股 (select)

**触发**: 用户想了解今天买什么股票 / 推荐股票 / 选股

```bash
python -m BBBIG.main select
```

**流程**: 大盘风控 → 板块资金流向 → 全量A股行情 → K线技术指标 → 多因子预筛选 TOP80 → AI 5维度评分精选 TOP10 → 回测验证排序

**输出**: TOP10 推荐股票，含评分明细、买入区间、目标价、止损价、仓位建议、盈利概率

### 2. 持仓分析 (analyze)

**触发**: 用户想分析手里的股票 / 该不该卖 / 操作建议

```bash
python -m BBBIG.main analyze
```

**前提**: 需先添加持仓

**输出**: 每只持仓股的趋势分析、技术指标、支撑/压力位、操作建议（持有/卖出/止损/加仓）

### 3. 添加持仓 (add)

**触发**: 用户说买了某只股票 / 添加持仓

```bash
python -m BBBIG.main add <代码> <成本价> [股数] [名称]
```

**示例**:
```bash
python -m BBBIG.main add 000001 12.50 1000 平安银行
python -m BBBIG.main add 600519 1700.00 100 贵州茅台
```

### 4. 移除持仓 (remove)

**触发**: 用户说卖了某只股票 / 移除持仓

```bash
python -m BBBIG.main remove <代码>
```

### 5. 查看持仓 (list)

**触发**: 用户想看持仓列表

```bash
python -m BBBIG.main list
```

### 6. 选股+分析一起执行 (run)

**触发**: 用户想全面分析 / 选股并分析持仓

```bash
python -m BBBIG.main run
```

### 7. AI 回测 (backtest)

**触发**: 用户想验证选股策略 / 回测

```bash
python -m BBBIG.main backtest 1,2,3
```

参数为回测周数列表（逗号分隔），表示往前推 N 周进行选股并验证

### 8. 纯量化回测 (qbacktest)

**触发**: 用户想快速验证因子效果 / 不用AI回测

```bash
python -m BBBIG.main qbacktest 1,2,3,4
```

不调用 AI，直接用多因子评分选股，快速迭代验证

### 9. 启动 Web 界面 (web)

**触发**: 用户想看可视化界面 / 启动网页

```bash
python -m BBBIG.main web [端口]
```

默认端口 9999，访问 http://localhost:9999

### 10. 启动定时调度 (serve)

**触发**: 用户想自动每天执行 / 后台运行

```bash
python -m BBBIG.main serve
```

每天 18:00 自动执行选股 + 持仓分析（仅交易日）

## 环境要求

- Python 3.8+
- 依赖: `pandas numpy requests schedule tornado tushare`
- 环境变量: `DEEPSEEK_API_KEY`, `TUSHARE_TOKEN`

## 输出说明

- 结果同时打印到终端 + 保存为 JSON 到 `BBBIG/data/results/`
- 日志保存在 `BBBIG/logs/`
- 持仓配置在 `BBBIG/data/portfolio.json`

## 使用注意

1. 选股命令耗时较长（约2~5分钟），需要获取大量行情数据 + 调用AI分析
2. 纯量化回测（qbacktest）不调 AI，速度较快
3. 需要在交易日运行才有最新数据
4. 所有分析结果仅供参考，不构成投资建议
