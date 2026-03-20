# Tushare Pro 数据接口 Skill

## 概述

本项目使用 Tushare Pro 作为 A 股数据源，所有 API 调用必须遵循以下规范，避免触发限流或权限错误。

---

## 一、限流机制（实测结论 2026-03-21）

Tushare 的限流**不是**简单的固定间隔限制，而是 **滑动时间窗口内累计请求数** 限制：

| 特性 | 详情 |
|------|------|
| 触发阈值 | 连续快速请求 ~5 次后触发 |
| 错误提示 | `"IP数量超限，最大数量为5个"` （**误导性文案**，实际是频率限流） |
| 惩罚期 | 触发后 **60 秒** 内所有请求都会失败 |
| 并发 | 多线程同时请求**极易**触发限流，必须串行 |
| 安全间隔 | 每次调用后等待 **≥ 2 秒** |

### 关键规则

1. **所有 Tushare API 调用必须走 `_call_api()` 方法**，禁止直接调用 `self.pro.xxx()`
2. **信号量设为 1**（`Semaphore(1)`），串行化所有请求
3. **调用间隔 ≥ 2.0 秒**（`_api_interval = 2.0`）
4. **限流重试退避 60 秒**，最多重试 3 次

### 标准调用模式

```python
import threading

# 全局信号量：串行化
_tushare_semaphore = threading.Semaphore(1)

class StockDataFetcher:
    def __init__(self):
        self._api_interval = 2.0

    def _call_api(self, fn, *args, max_retries=3, **kwargs):
        for attempt in range(max_retries):
            with _tushare_semaphore:
                try:
                    result = fn(*args, **kwargs)
                    time.sleep(self._api_interval)
                    return result
                except Exception as e:
                    err = str(e)
                    if "IP数量超限" in err or ("IP" in err and "超限" in err):
                        time.sleep(60)  # 惩罚期冷却
                    elif "每分钟" in err or "频次" in err:
                        time.sleep(30)
                    else:
                        raise
        # 最后一次不捕获
        with _tushare_semaphore:
            result = fn(*args, **kwargs)
            time.sleep(self._api_interval)
            return result

    # 正确 ✓
    df = self._call_api(self.pro.daily, ts_code='000001.SZ', start_date='20260101')

    # 错误 ✗ — 绕过并发控制和限流保护
    df = self.pro.daily(ts_code='000001.SZ', start_date='20260101')
```

---

## 二、积分与接口权限

当前 Token 积分：**2000 分**

| 接口 | 函数名 | 所需积分 | 可用 |
|------|--------|----------|------|
| 日线行情 | `daily` | 120 | ✓ |
| 每日指标 | `daily_basic` | 2000 | ✓ |
| 复权因子 | `adj_factor` | 120 | ✓ |
| 股票基本信息 | `stock_basic` | 120 | ✓ |
| 交易日历 | `trade_cal` | 120 | ✓ |
| 指数日线 | `index_daily` | 120 | ✓ |
| 通用行情 | `pro_bar` | 120 | ✓ |
| 个股资金流向 | `moneyflow` | 2000 | ✓ |
| 同花顺行业资金流 | `moneyflow_ind_ths` | 2000 | ✓ |
| 概念板块分类 | `concept` | 2000 | ✓ |
| 概念板块成分 | `concept_detail` | 2000 | ✓ |
| 涨跌停统计 | `limit_list_d` | 2000 | ✓ |
| 每日停复牌 | `suspend_d` | 120 | ✓ |
| 财务指标 | `fina_indicator` | 5000 | ✗ |
| 资产负债表 | `balancesheet` | 5000 | ✗ |
| 利润表 | `income` | 5000 | ✗ |
| 龙虎榜 | `top_list` | 5000 | ✗ |

> 使用不够积分的接口会报 `"抱歉，您没有访问该接口的权限"`，代码中应做降级处理。

---

## 三、数据缓存策略

所有从 Tushare 获取的数据应缓存到本地 SQLite（`db_cache`），避免重复调用：

1. **查缓存** → 有数据直接返回
2. **缺失时** → 调用 Tushare API 获取
3. **写入缓存** → 下次直接读本地

```python
def _ensure_daily(self, trade_date):
    if db_cache.has_daily(trade_date):
        return  # 缓存命中，不调 API
    df = self._call_api(self.pro.daily, trade_date=trade_date)
    if df is not None and not df.empty:
        db_cache.save_daily(df)
```

---

## 四、环境配置

```bash
# 环境变量（不要硬编码到代码中）
export TUSHARE_TOKEN=你的token
export DEEPSEEK_API_KEY=你的key
```

- Token 通过 `BBBIG/config.py` 中的 `TUSHARE_TOKEN` 读取
- **严禁在代码中硬编码 token/key**，提交前务必检查

---

## 五、测试工具

限速测试脚本位于 `BBBIG/testcase/test_tushare_ratelimit.py`，支持 4 项测试：

```bash
python3 -m BBBIG.testcase.test_tushare_ratelimit --test all       # 全部测试
python3 -m BBBIG.testcase.test_tushare_ratelimit --test interval   # 间隔测试
python3 -m BBBIG.testcase.test_tushare_ratelimit --test concurrent # 并发测试
python3 -m BBBIG.testcase.test_tushare_ratelimit --test burst      # 突发测试
python3 -m BBBIG.testcase.test_tushare_ratelimit --test cooldown   # 冷却恢复测试
```

修改限流参数后应重新跑测试验证。

---

## 六、常见错误及处理

| 错误信息 | 原因 | 处理 |
|----------|------|------|
| `IP数量超限，最大数量为5个` | 频率限流（非IP池满） | 等待 60s 冷却后重试 |
| `每分钟最多访问N次` | 分钟级频率限制 | 等待 30s 后重试 |
| `您没有访问该接口的权限` | 积分不足 | 降级处理，用替代数据源或默认值 |
| `抱歉，该接口仅限xxx积分以上` | 同上 | 同上 |
