#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tushare API 限速测试脚本
=========================
测试目标：找到 Tushare 最小安全调用间隔，避免触发 "IP数量超限" 限流。

用法：
  # 需设置环境变量或直接传参
  export TUSHARE_TOKEN=你的token
  python3 -m BBBIG.testcase.test_tushare_ratelimit

  # 或直接指定 token
  python3 -m BBBIG.testcase.test_tushare_ratelimit --token YOUR_TOKEN

  # 只跑某个测试
  python3 -m BBBIG.testcase.test_tushare_ratelimit --test interval
  python3 -m BBBIG.testcase.test_tushare_ratelimit --test concurrent
  python3 -m BBBIG.testcase.test_tushare_ratelimit --test burst

测试结论（2026-03-21 于 14.116.239.34，120积分账户）：
  ══════════════════════════════════════════════════════════════════════

  一、限流机制
  ──────────
  Tushare 的 "IP数量超限，最大数量为5个" 报错实际是 **频率限流**，不是 IP 池满。
  限流方式为 "滑动时间窗口内累计请求数"，而非简单的固定间隔。

  二、实测数据
  ──────────
  测试1（间隔测试，每组5次请求）：
    间隔 0.0s → 前4~5次成功，随后触发限流
    间隔 0.3s → 5次全部成功（冷却后）
    间隔 0.5s → 触发限流（因前面已累计大量请求）
    间隔 1.0s → 触发限流（仍处于惩罚期）
    间隔 2.0s → 5次全部成功（冷却后稳定通过）
    间隔 3.0s → 5次全部成功

  测试2（并发测试，4线程同时发）：
    冷却后并发 4 线程 → 全部失败，并发极易触发限流

  测试3（突发测试，无间隔连续10次）：
    冷却后连续请求 → 第5~6次开始触发限流

  测试4（冷却恢复测试，触发限流后等不同时间）：
     5s 冷却 → 仍限流
    10s 冷却 → 仍限流
    15s 冷却 → 仍限流
    20s 冷却 → 仍限流
    30s 冷却 → 仍限流
    45s 冷却 → 仍限流
    60s 冷却 → 恢复成功 ✓（连续验证3次均成功）

  三、关键结论
  ──────────
  1. 冷却后可连续快速发 4~5 次请求不报错
  2. 第 5 次左右触发 "IP数量超限" 限流（错误提示有误导性，实际是频率限流）
  3. 触发限流后进入惩罚期，期间任何间隔的请求都会失败
  4. 惩罚冷却期约 60s，60s 后完全恢复
  5. 并发请求（多线程同时发）极易触发限流，必须串行

  四、安全配置建议（data_fetcher.py）
  ──────────────────────────────────
    _api_interval      = 2.0           # 每次调用后等 2s，确保滑动窗口内请求数不超限
    _tushare_semaphore = Semaphore(1)  # 串行化所有 Tushare 调用，避免并发
    _call_api 重试退避 = 60s           # 触发限流后至少等 60s 再重试

  ══════════════════════════════════════════════════════════════════════
"""
import os
import sys
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import tushare as ts
except ImportError:
    print("请先安装 tushare: pip install tushare")
    sys.exit(1)


# ─── 工具函数 ──────────────────────────────────────────────────────────────────

def make_call(pro, label=""):
    """单次轻量API调用，返回 (成功, 耗时, 消息)"""
    t0 = time.time()
    try:
        df = pro.trade_cal(exchange='SSE', start_date='20260101', end_date='20260110')
        elapsed = time.time() - t0
        return True, elapsed, f"{label} OK ({len(df)}条, {elapsed:.3f}s)"
    except Exception as e:
        elapsed = time.time() - t0
        return False, elapsed, f"{label} FAIL: {e} ({elapsed:.3f}s)"


def print_header(title):
    print(f"\n{'=' * 64}")
    print(f"  {title}")
    print(f"{'=' * 64}")


# ─── 测试1：不同间隔的串行调用 ─────────────────────────────────────────────────

def test_interval(pro, intervals=None):
    """测试不同间隔下的调用成功率"""
    if intervals is None:
        intervals = [0.0, 0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]

    print_header("测试1: 不同间隔的串行调用（每个间隔连续5次）")
    print(f"  待测间隔: {intervals}")

    results = {}
    for interval in intervals:
        print(f"\n  --- 间隔 {interval:.1f}s ---")
        # 每轮开始前冷却3秒，避免上一轮残留影响
        time.sleep(3)

        success = 0
        total = 5
        for i in range(total):
            ok, elapsed, msg = make_call(pro, f"    [{i+1}/{total}]")
            print(msg)
            if ok:
                success += 1
            else:
                # 一旦失败就停止本轮，节省时间
                print(f"    → 间隔 {interval:.1f}s 第{i+1}次失败，跳过剩余")
                break
            if i < total - 1:
                time.sleep(interval)

        rate = success / total * 100
        results[interval] = (success, total, rate)
        print(f"  结果: {success}/{total} ({rate:.0f}%)")

    # 汇总
    print_header("间隔测试汇总")
    print(f"  {'间隔(s)':<10} {'成功/总数':<12} {'成功率':<10} {'状态'}")
    print(f"  {'-'*10} {'-'*12} {'-'*10} {'-'*10}")
    safe_interval = None
    for interval, (s, t, r) in sorted(results.items()):
        status = "✓ 安全" if r == 100 else "✗ 不安全"
        print(f"  {interval:<10.1f} {f'{s}/{t}':<12} {r:<10.0f} {status}")
        if r == 100 and safe_interval is None:
            safe_interval = interval

    if safe_interval is not None:
        print(f"\n  >>> 最小安全间隔: {safe_interval:.1f}s")
        print(f"  >>> 建议配置: _api_interval = {safe_interval}（或稍大一点留余量）")
    else:
        print("\n  >>> 所有间隔都有失败，建议增大间隔或检查积分等级")

    return results


# ─── 测试2：并发调用 ──────────────────────────────────────────────────────────

def test_concurrent(pro, workers_list=None):
    """测试不同并发数下的成功率"""
    if workers_list is None:
        workers_list = [1, 2, 3, 4]

    print_header("测试2: 并发调用（每种并发度发8次请求）")

    results = {}
    for workers in workers_list:
        print(f"\n  --- {workers} 线程并发 ---")
        time.sleep(3)  # 冷却

        successes = 0
        total = 8
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(make_call, pro, f"    [线程{i+1}]") for i in range(total)]
            for f in as_completed(futures):
                ok, _, msg = f.result()
                print(msg)
                if ok:
                    successes += 1

        rate = successes / total * 100
        results[workers] = (successes, total, rate)
        print(f"  结果: {successes}/{total} ({rate:.0f}%)")

    # 汇总
    print_header("并发测试汇总")
    print(f"  {'并发数':<10} {'成功/总数':<12} {'成功率':<10} {'状态'}")
    print(f"  {'-'*10} {'-'*12} {'-'*10} {'-'*10}")
    for workers, (s, t, r) in sorted(results.items()):
        status = "✓ 可用" if r == 100 else "✗ 有失败"
        print(f"  {workers:<10} {f'{s}/{t}':<12} {r:<10.0f} {status}")

    return results


# ─── 测试3：突发请求（burst）────────────────────────────────────────────────

def test_burst(pro):
    """测试连续快速请求多少次后触发限流"""
    print_header("测试3: 突发请求（无间隔连续调用，看第几次触发限流）")
    time.sleep(3)  # 冷却

    max_calls = 20
    first_fail = None
    for i in range(max_calls):
        ok, elapsed, msg = make_call(pro, f"  [{i+1}/{max_calls}]")
        print(msg)
        if not ok:
            if first_fail is None:
                first_fail = i + 1
            # 连续失败3次就停
            if i + 1 - first_fail >= 2:
                print(f"\n  >>> 连续失败，停止测试")
                break

    if first_fail:
        print(f"\n  >>> 第 {first_fail} 次调用触发限流（前 {first_fail - 1} 次成功）")
    else:
        print(f"\n  >>> {max_calls} 次全部成功，未触发限流")


# ─── 测试4：冷却恢复时间 ──────────────────────────────────────────────────────

def test_cooldown(pro, cooldowns=None):
    """触发限流后，测试需要冷却多久才能恢复"""
    if cooldowns is None:
        cooldowns = [5, 10, 15, 20, 30, 45, 60]

    print_header("测试4: 冷却恢复时间（先触发限流，再等不同时间后重试）")

    # 先触发限流：快速连续请求
    print("  故意触发限流...")
    for i in range(15):
        ok, _, msg = make_call(pro, f"  [触发{i+1}]")
        print(msg)
        if not ok:
            break

    # 确认已被限流
    ok, _, msg = make_call(pro, "  [确认限流]")
    print(msg)
    if ok:
        print("  未触发限流，无法进行冷却测试")
        return {}

    results = {}
    for wait in cooldowns:
        print(f"\n  等待 {wait}s 冷却中...", end="", flush=True)
        time.sleep(wait)
        print(" 尝试请求:")
        ok, _, msg = make_call(pro, f"    [冷却{wait}s]")
        print(msg)
        results[wait] = ok
        if ok:
            print(f"\n  >>> {wait}s 冷却后恢复成功！")
            # 确认不是偶然成功：再连续3次
            all_ok = True
            for j in range(3):
                time.sleep(0.3)
                ok2, _, msg2 = make_call(pro, f"    [验证{j+1}/3]")
                print(msg2)
                if not ok2:
                    all_ok = False
                    break
            if all_ok:
                print(f"\n  >>> 确认: 冷却 {wait}s 后完全恢复")
                break
            else:
                print(f"    冷却 {wait}s 不稳定，继续测试更长时间")
                results[wait] = False

    # 汇总
    print_header("冷却测试汇总")
    recovery_time = None
    for wait, ok in sorted(results.items()):
        status = "✓ 恢复" if ok else "✗ 仍限流"
        print(f"  {wait:>4}s  {status}")
        if ok and recovery_time is None:
            recovery_time = wait

    if recovery_time:
        print(f"\n  >>> 限流冷却恢复时间: {recovery_time}s")
    else:
        print(f"\n  >>> 在测试范围内未恢复，可能需要更长冷却时间")

    return results


# ─── 主入口 ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tushare API 限速测试")
    parser.add_argument("--token", type=str, default=None, help="Tushare token（或设置 TUSHARE_TOKEN 环境变量）")
    parser.add_argument("--test", type=str, default="all",
                        choices=["all", "interval", "concurrent", "burst", "cooldown"],
                        help="运行哪个测试（默认 all）")
    args = parser.parse_args()

    token = args.token or os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        print("错误: 请通过 --token 或 TUSHARE_TOKEN 环境变量提供 token")
        sys.exit(1)

    ts.set_token(token)
    pro = ts.pro_api()

    print("Tushare 限速测试")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Token: {token[:8]}...{token[-8:]}")

    # 先验证 token 可用
    ok, _, msg = make_call(pro, "预检")
    print(f"  预检: {msg}")
    if not ok:
        print("\n预检失败，token 或 IP 不可用，无法继续测试。")
        sys.exit(1)
    time.sleep(3)

    if args.test in ("all", "interval"):
        test_interval(pro)

    if args.test in ("all", "concurrent"):
        test_concurrent(pro)

    if args.test in ("all", "burst"):
        test_burst(pro)

    if args.test in ("all", "cooldown"):
        test_cooldown(pro)

    print_header("完成")
    print("  所有测试已完成，请根据上方汇总调整 data_fetcher.py 中的参数：")
    print("    _api_interval      → 最小安全间隔")
    print("    _tushare_semaphore → Semaphore(1) 表示串行")
    print("    _call_api 重试退避 → 参考冷却恢复时间")


if __name__ == "__main__":
    main()
