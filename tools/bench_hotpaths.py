#!/usr/bin/env python3
"""热点基准 + 确定性性能契约（2.0 之后「梯队 0」治理能力）。

为什么这样设计：CI 机器的绝对耗时不可跨环境比较，直接用「毫秒阈值」做门禁
必然误报。所以本脚本分两层：

1. **确定性性能契约（硬断言，完全不依赖计时）**：用 spy store 记录事件序列，
   证明 B4 ``execute_many`` 的多条写入是「1 连接 / N 条语句 / 1 提交」，
   而逐条 ``execute`` 仍是「N 连接 / N 提交」。连接与事务次数是性能本质，
   在任何机器上都可复现。
2. **计时报告（归一化比值）**：把每个热点的 ns/op 除以同一进程内参考热点的
   ns/op，得到跨机器可比的比值；与基线比值比较，容差 3.0x，只抓数量级退化
   （例如 O(n) 变 O(n^2)），不因 CPU 型号 / Python 补丁版 / 负载抖动误报。

只依赖标准库（``sqlite3`` / ``time`` / ``importlib``），不 import 应用包，
不修改任何运行时源码。

用法::

    python tools/bench_hotpaths.py [ROOT]           # 基准 + 契约 + 门槛比对
    python tools/bench_hotpaths.py [ROOT] --init    # 重新采集比值基线
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time

BASELINE_REL = "tools/bench_baseline.json"
RATIO_TOLERANCE = 3.0
MIN_SECONDS = 0.06
REFERENCE = "sanitize_html_short"

RICH_SHORT = '<p>Hello <b>world</b><br>next &amp; last</p>'
RICH_LONG = "".join(
    '<p class="ql-align-center">第%d段 <span style="color:#ff0000">内容</span> '
    '<a href="https://example.com/%d">链接</a></p>' % (i, i)
    for i in range(40)
)
PLAIN_SHORT = "hello world"


def parse_args(argv):
    root, flags = None, set()
    for arg in argv:
        if arg.startswith("--"):
            flags.add(arg)
        elif root is None:
            root = arg
    return os.path.abspath(root or "."), flags


def load_security_utils(root):
    """单文件加载，避免触发包级 import（保持纯标准库）。"""
    path = os.path.join(root, "app", "core", "security_utils.py")
    spec = importlib.util.spec_from_file_location("_bench_security_utils", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 确定性性能契约：事件序列 spy（不计时） ----------

class _SpyCursor:
    def __init__(self, events):
        self.events = events

    def execute(self, sql, params=()):
        self.events.append(("sql", sql))


class _SpyConn:
    def __init__(self, events):
        self.events = events

    def cursor(self):
        self.events.append(("cursor",))
        return _SpyCursor(self.events)

    def commit(self):
        self.events.append(("commit",))

    def rollback(self):
        self.events.append(("rollback",))


class _SpyStore:
    def __init__(self):
        self.events = []

    def connect(self):
        self.events.append(("connect",))
        return _ConnCtx(_SpyConn(self.events))


class _ConnCtx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def old_execute_many(store, statements):
    """B3-B/B4 之前逐条提交的骨架（oracle）。"""
    for sql, params in statements:
        with store.connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()


def new_execute_many(store, statements):
    """抽离后的单连接单事务骨架。"""
    with store.connect() as conn:
        cur = conn.cursor()
        for sql, params in statements:
            cur.execute(sql, params)
        conn.commit()


def deterministic_contracts(fails):
    statements = [("CREATE TABLE IF NOT EXISTS t%d (id INTEGER)" % i, ()) for i in range(6)]
    old_store, new_store = _SpyStore(), _SpyStore()
    old_execute_many(old_store, statements)
    new_execute_many(new_store, statements)

    old_c = [e for e in old_store.events if e[0] == "connect"]
    new_c = [e for e in new_store.events if e[0] == "connect"]
    old_commit = [e for e in old_store.events if e[0] == "commit"]
    new_commit = [e for e in new_store.events if e[0] == "commit"]
    old_sql = [e[1] for e in old_store.events if e[0] == "sql"]
    new_sql = [e[1] for e in new_store.events if e[0] == "sql"]
    expected_sql = [s for s, _ in statements]

    checks = [
        ("逐条 execute 保持 6 次连接 / 6 次提交（原语义未变）",
         len(old_c) == 6 and len(old_commit) == 6),
        ("execute_many 收敛为 1 次连接 / 1 次提交（B4 收益）",
         len(new_c) == 1 and len(new_commit) == 1),
        ("单事务批次 DDL 顺序与输入逐字一致（B3-B oracle）",
         new_sql == expected_sql and old_sql == expected_sql),
        ("单事务批次 cursor 只创建 1 次",
         len([e for e in new_store.events if e[0] == "cursor"]) == 1),
    ]
    print("\n== 确定性性能契约（不依赖计时）==")
    for name, ok in checks:
        print("  %s  %s" % ("PASS" if ok else "FAIL", name))
        if not ok:
            fails.append(name)
    return 1, (6, 6), (1, 1)


# ---------- 计时报告（归一化比值） ----------

def measure(fn, ops):
    """返回 ns/op；取多轮最小耗时，抑制 GC / 调度抖动。"""
    fn()  # 预热
    best = None
    deadline = time.perf_counter() + MIN_SECONDS
    while time.perf_counter() < deadline:
        t0 = time.perf_counter()
        fn()
        dt = time.perf_counter() - t0
        if best is None or dt < best:
            best = dt
    return (best or 0.0) * 1e9 / ops


def build_hotspots(secu):
    """每个热点: (name, callable, ops, 是否纳入比值门槛)。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE bench (id INTEGER PRIMARY KEY, v TEXT)")

    def sql_single():
        conn.execute("DELETE FROM bench")
        for i in range(50):
            conn.execute("INSERT INTO bench (v) VALUES (?)", ("x%d" % i,))
            conn.commit()

    def sql_batch():
        conn.execute("DELETE FROM bench")
        for i in range(50):
            conn.execute("INSERT INTO bench (v) VALUES (?)", ("x%d" % i,))
        conn.commit()

    def sql_ddl():
        for i in range(20):
            conn.execute("CREATE TABLE IF NOT EXISTS d%d (id INTEGER)" % i)
        conn.commit()

    return [
        ("sanitize_html_short", lambda: secu.sanitize_html(PLAIN_SHORT), 1, True),
        ("sanitize_html_rich", lambda: secu.sanitize_html(RICH_SHORT), 1, True),
        ("to_plain_text_rich", lambda: secu.to_plain_text(RICH_SHORT), 1, True),
        ("to_plain_text_escaped", lambda: secu.to_plain_text("&lt;p&gt;测试推送&lt;/p&gt;"), 1, True),
        ("sanitize_rich_short", lambda: secu.sanitize_rich_html(RICH_SHORT), 1, True),
        ("sanitize_rich_long", lambda: secu.sanitize_rich_html(RICH_LONG), 1, True),
        ("sqlite_single_commit", sql_single, 50, False),
        ("sqlite_batch_commit", sql_batch, 50, False),
        ("sqlite_ddl_20", sql_ddl, 20, False),
    ]


def main():
    root, flags = parse_args(sys.argv[1:])
    if not os.path.isfile(os.path.join(root, "app", "core", "security_utils.py")):
        print("FAIL: ROOT 不是仓库根目录: %s" % root)
        return 1

    fails = []
    deterministic_contracts(fails)

    secu = load_security_utils(root)
    hotspots = build_hotspots(secu)
    ns = {}
    for name, fn, ops, _ in hotspots:
        ns[name] = measure(fn, ops)

    ref = ns.get(REFERENCE) or 1.0
    ratios = {name: (value / ref if ref else 0.0) for name, value in ns.items()}
    baseline_path = os.path.join(root, BASELINE_REL)

    print("\n== 热点基准（ns/op，取多轮最小值）==")
    print("%-28s %6s %14s %14s  %s" % ("hotspot", "ops", "ns/op", "ratio", "gate"))
    for name, _, ops, gated in hotspots:
        print("%-28s %6d %14.1f %14.3f  %s"
              % (name, ops, ns[name], ratios[name],
                 "<= %.1fx base" % RATIO_TOLERANCE if gated else "report only"))

    if "--init" in flags:
        if fails:
            print("\nFAIL: 确定性性能契约未通过，拒绝生成比值基线")
            return 1
        payload = {
            "reference": REFERENCE,
            "ratio_tolerance": RATIO_TOLERANCE,
            "ratios": {k: round(v, 4) for k, v in ratios.items()},
            "note": "只存归一化比值，不存绝对耗时（跨机器不可比）。",
        }
        with open(baseline_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        print("\n比值基线已写入 %s" % BASELINE_REL)
        return 0

    if not os.path.isfile(baseline_path):
        print("\nFAIL: 缺少比值基线 %s，请先执行 --init" % BASELINE_REL)
        return 1
    with open(baseline_path, encoding="utf-8") as f:
        baseline = json.load(f)
    base_ratios = baseline.get("ratios", {})

    print("\n== 归一化比值门槛（容差 %.1fx，只抓数量级退化）==" % RATIO_TOLERANCE)
    for name, _, _, gated in hotspots:
        if not gated:
            continue
        b = base_ratios.get(name)
        if b is None:
            print("  NEW   %-28s base=n/a  now=%.3f（新增热点，不 gate）" % (name, ratios[name]))
            continue
        ok = ratios[name] <= b * RATIO_TOLERANCE
        print("  %s  %-28s base=%.3f  now=%.3f  limit=%.3f"
              % ("PASS" if ok else "FAIL", name, b, ratios[name], b * RATIO_TOLERANCE))
        if not ok:
            fails.append("%s 比值退化 %.3f -> %.3f（上限 %.3f）"
                         % (name, b, ratios[name], b * RATIO_TOLERANCE))

    print("\nfailed: %d" % len(fails))
    if fails:
        for f in fails:
            print("  - " + f)
        return 1
    print("BENCH HOTPATHS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
