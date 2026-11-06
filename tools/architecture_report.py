#!/usr/bin/env python3
"""架构指标报告 + 不恶化门槛（2.0 之后「梯队 0」治理能力）。

目的：把「架构是否在变好」从主观判断变成可机器判定、可 CI 拦截的指标。
只做只读静态分析（AST + 正则），不执行应用代码，不依赖任何第三方库，
也**不修改任何运行时源码**。

用法::

    python tools/architecture_report.py [ROOT]            # 报告 + 门槛比对
    python tools/architecture_report.py [ROOT] --init     # 重新采集并写入基线

门槛设计（刻意只 gate「坏味道类」，不 gate 规模类）：
  * 规模类（runtime_py_lines / template_lines / js_lines 等）只报告 delta；
    正常新增功能必然让行数增长，硬门槛会让 CI 永久变红。
  * 坏味道类设「不得增加」：内联 <script> 行数与块数、循环内 DB 访问、
    最长函数行数。这些是 C 批 / P 批真正要收敛的对象。
  * ``ddl_helper_calls`` 设「不得减少」，防止 B3-B 的去重成果被回退。

指标口径（必须与基线采集时完全一致，否则门槛失真）：
  * 运行时 Python 只统计 ``app/**/*.py``；模板统计 ``templates/**/*.html``；
    JS 统计 ``static/js/**/*.js``。tests / tools 不计入规模指标。
  * ``inline_script_*``：模板中不带 ``src=`` 的 <script>...</script> 块，
    按正则配对统计块数与块内行数。
  * ``loop_db_calls``：``app/**/*.py`` 的 AST 中，位于 for/while 体内的
    ``execute / executemany / execute_many / fetchone / fetchall / connect /
    cursor / commit / rollback`` 调用节点数，按节点去重（嵌套循环不重复计）。
  * ``longest_function_lines``：全部函数 / 方法定义中最大的
    ``end_lineno - lineno + 1``。
  * ``ensure_defs`` / ``ensure_calls``：AST 中名称以 ``ensure_`` 开头的
    顶层/嵌套函数定义与真实调用（``ast.Call``），不使用裸子串。
"""
from __future__ import annotations

import ast
import glob
import json
import os
import re
import sys

BASELINE_REL = "tools/architecture_baseline.json"
SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
DB_CALLS = {
    "execute", "executemany", "execute_many", "fetchone", "fetchall",
    "connect", "cursor", "commit", "rollback",
}
GATES_MAX = (
    "inline_script_lines",
    "inline_script_blocks",
    "loop_db_calls",
    "longest_function_lines",
)
GATES_MIN = ("ddl_helper_calls",)


def parse_args(argv):
    root, flags = None, set()
    for arg in argv:
        if arg.startswith("--"):
            flags.add(arg)
        elif root is None:
            root = arg
    return os.path.abspath(root or "."), flags


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def lines(text):
    return len(text.splitlines())


def call_name(node):
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def compute(root):
    runtime = sorted(glob.glob(os.path.join(root, "app", "**", "*.py"), recursive=True))
    templates = sorted(glob.glob(os.path.join(root, "templates", "**", "*.html"), recursive=True))
    js = sorted(glob.glob(os.path.join(root, "static", "js", "**", "*.js"), recursive=True))

    runtime_lines = sum(lines(read_text(p)) for p in runtime)
    tpl_lines = sum(lines(read_text(p)) for p in templates)
    js_lines = sum(lines(read_text(p)) for p in js)

    inline_blocks = 0
    inline_lines = 0
    tpl_with_inline = 0
    for p in templates:
        blocks = [m for m in SCRIPT_RE.finditer(read_text(p))
                  if "src=" not in m.group(1).lower()]
        if blocks:
            tpl_with_inline += 1
        inline_blocks += len(blocks)
        inline_lines += sum(lines(m.group(2)) for m in blocks)

    ensure_defs = 0
    ensure_calls = 0
    loop_db_calls = 0
    ddl_helper_calls = 0
    execute_many_calls = 0
    provider_assembly_points = 0
    longest = []
    for p in runtime:
        text = read_text(p)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        seen_loop_nodes = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                span = (n.end_lineno or n.lineno) - n.lineno + 1
                longest.append((span, rel, n.name))
                if n.name.startswith("ensure_"):
                    ensure_defs += 1
            if isinstance(n, ast.Call):
                fname = call_name(n)
                if fname:
                    if fname.startswith("ensure_"):
                        ensure_calls += 1
                    if fname == "ensure_registered_tables":
                        ddl_helper_calls += 1
                    if fname == "execute_many":
                        execute_many_calls += 1
                    if fname in ("set_dependency_providers", "apply_dependency_providers"):
                        provider_assembly_points += 1
            if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
                for sub in ast.walk(n):
                    if not isinstance(sub, ast.Call) or id(sub) in seen_loop_nodes:
                        continue
                    if call_name(sub) in DB_CALLS:
                        seen_loop_nodes.add(id(sub))
                        loop_db_calls += 1

    longest.sort(reverse=True)
    return {
        "runtime_py_files": len(runtime),
        "runtime_py_lines": runtime_lines,
        "template_files": len(templates),
        "template_lines": tpl_lines,
        "js_files": len(js),
        "js_lines": js_lines,
        "templates_with_inline_script": tpl_with_inline,
        "inline_script_blocks": inline_blocks,
        "inline_script_lines": inline_lines,
        "ensure_defs": ensure_defs,
        "ensure_calls": ensure_calls,
        "loop_db_calls": loop_db_calls,
        "longest_function_lines": longest[0][0] if longest else 0,
        "top5_longest": [
            {"lines": a, "file": b, "name": c} for a, b, c in longest[:5]
        ],
        "ddl_helper_calls": ddl_helper_calls,
        "execute_many_calls": execute_many_calls,
        "provider_assembly_points": provider_assembly_points,
    }


def main():
    root, flags = parse_args(sys.argv[1:])
    if not os.path.isdir(os.path.join(root, "app")):
        print("FAIL: ROOT 不是仓库根目录: %s" % root)
        return 1

    current = compute(root)
    baseline_path = os.path.join(root, BASELINE_REL)

    if "--init" in flags:
        with open(baseline_path, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        print("基线已写入 %s" % BASELINE_REL)
        print(json.dumps(current, ensure_ascii=False, indent=1))
        return 0

    if not os.path.isfile(baseline_path):
        print("FAIL: 缺少基线 %s，请先执行 --init" % BASELINE_REL)
        return 1
    with open(baseline_path, encoding="utf-8") as f:
        baseline = json.load(f)

    print("== 架构指标报告（基线 vs 当前）==")
    print("%-32s %10s %10s %9s  %s" % ("metric", "baseline", "current", "delta", "gate"))
    scalar_keys = [k for k in current if k != "top5_longest"]
    for k in scalar_keys:
        b, c = baseline.get(k), current[k]
        if not isinstance(b, (int, float)):
            print("%-32s %10s %10s %9s" % (k, "-", c, "-"))
            continue
        delta = c - b
        gate = "<= base" if k in GATES_MAX else (">= base" if k in GATES_MIN else "-")
        print("%-32s %10s %10s %+9d  %s" % (k, b, c, delta, gate))

    print("\n最长函数 Top5（当前）:")
    for item in current["top5_longest"]:
        print("  %5d  %-70s %s" % (item["lines"], item["file"], item["name"]))

    fails = []
    for k in GATES_MAX:
        if current[k] > baseline.get(k, current[k]):
            fails.append("%s 恶化: %s -> %s（门槛：不得增加）" % (k, baseline.get(k), current[k]))
    for k in GATES_MIN:
        if current[k] < baseline.get(k, current[k]):
            fails.append("%s 回退: %s -> %s（门槛：不得减少）" % (k, baseline.get(k), current[k]))

    print("\n门槛: %d 项，失败: %d" % (len(GATES_MAX) + len(GATES_MIN), len(fails)))
    if fails:
        for f in fails:
            print("  FAIL  " + f)
        print("\n如属合法增长，请显式执行 `python tools/architecture_report.py . --init`")
        print("重新采集基线，并在提交说明里写清增长原因（基线变更必须可 review）。")
        return 1
    print("ARCHITECTURE GATES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
