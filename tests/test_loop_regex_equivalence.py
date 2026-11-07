#!/usr/bin/env python3
"""2.1 批次 1 等价性回归：16 处循环内字面量正则 → 模块级 re.compile。

证明分两层，缺一不可：

1. **行为层**：改代码前采集的 ``tests/loop_regex_golden.json`` 存了 16 组
   pattern/flags/输入/oracle 输出。测试用 ``re.compile(pattern, flags)`` 重放，
   断言输出逐字相同；同时再与模块级 ``re.op(pattern, ..., flags=...)`` 比对，
   证明「预编译对象调用」与「原调用」在标准库语义上等价。
2. **形态层**：AST 断言每个模块级常量的 ``re.compile`` 参数与 golden 的
   pattern/flags 一致、常量确实在函数体内被真实调用（``ast.Call`` +
   ``ast.Attribute``，不用裸子串）、循环体内已不存在 ``re.op("字面量")``。

只依赖标准库，不 import 应用包（避免 fastapi 等应用依赖）。
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
GOLDEN_PATH = os.path.join(HERE, "loop_regex_golden.json")

with open(GOLDEN_PATH, encoding="utf-8") as f:
    GOLDEN = json.load(f)
SPECS = GOLDEN["specs"]

# golden 只描述行为；常量名属于代码形态，单独维护，避免把 spec 与实现耦合。
CONST_BY_ID = {
    "dedupe_quality_markers": "_QUALITY_MARKER_RE",
    "stats_helpers_season_s": "_SEASON_S_RE",
    "stats_helpers_season_cn_num": "_SEASON_CN_NUM_RE",
    "stats_helpers_season_cn_hanzi": "_SEASON_CN_HANZI_RE",
    "user_details_datetime_full": "_PLAY_DATETIME_RE",
    "user_details_date_only": "_PLAY_DATE_RE",
    "hdhive_checkin_time": "_CHECKIN_TIME_RE",
    "hdhivesign_checkin_time": "_CHECKIN_TIME_RE",
    "hdhivesign_server_ref": "_SERVER_REF_RE",
    "library_cover_safe_name": "_SAFE_FILENAME_RE",
    "notify_quality_empty_sep": "_QUALITY_EMPTY_SEP_RE",
    "notify_quality_empty_eol": "_QUALITY_EMPTY_EOL_RE",
    "notify_quality_blank_pipe": "_QUALITY_BLANK_PIPE_RE",
    "notify_encoding_empty_eol": "_ENCODING_EMPTY_EOL_RE",
    "notify_encoding_blank_eol": "_ENCODING_BLANK_EOL_RE",
    "notify_icon_only_line": "_ICON_ONLY_LINE_RE",
}

FAILS = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  " + name)
    else:
        FAILS.append(name + ("  " + detail if detail else ""))
        print("  FAIL  " + name + ("  " + detail if detail else ""))


def flag_value(name):
    return 0 if not name else getattr(re, name, 0)


def normalize(value, op):
    if op in ("sub", "findall") or value is None:
        return value
    return {"group0": value.group(0), "groups": list(value.groups()),
            "span": list(value.span())}


def run_compiled(compiled, op, text, spec):
    if op == "sub":
        return compiled.sub(spec["repl"], text)
    if op == "findall":
        return compiled.findall(text)
    if op == "search":
        return compiled.search(text)
    if op == "match":
        return compiled.match(text)
    raise ValueError(op)


def run_module_level(op, text, spec):
    flags = flag_value(spec["flags"])
    pattern = spec["pattern"]
    if op == "sub":
        return re.sub(pattern, spec["repl"], text, flags=flags)
    if op == "findall":
        return re.findall(pattern, text, flags=flags)
    if op == "search":
        return re.search(pattern, text, flags=flags)
    if op == "match":
        return re.match(pattern, text, flags=flags)
    raise ValueError(op)


print("== 1. 改代码前采集的 golden 行为重放 ==")
check("golden 覆盖 16 组 spec 且常量名映射完整",
      len(SPECS) == 16 and set(CONST_BY_ID) == {s["id"] for s in SPECS},
      "specs=%d consts=%d" % (len(SPECS), len(CONST_BY_ID)))

for spec in SPECS:
    compiled = re.compile(spec["pattern"], flag_value(spec["flags"]))
    diffs = []
    for text, expected in zip(spec["inputs"], spec["oracle"]):
        got = normalize(run_compiled(compiled, spec["op"], text, spec), spec["op"])
        legacy = normalize(run_module_level(spec["op"], text, spec), spec["op"])
        if got != expected or legacy != expected:
            diffs.append((text[:40], got, legacy, expected))
    check("golden %-36s %d 组输入逐字一致" % (spec["id"], len(spec["inputs"])),
          not diffs, str(diffs[:1]))

# ------------------------------------------------------------ 2. AST 形态契约
print("\n== 2. 源码形态契约（AST 真实节点，不用裸子串） ==")
source_cache = {}


def source(rel):
    if rel not in source_cache:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            source_cache[rel] = f.read()
    return source_cache[rel]


def compile_consts(rel):
    """模块级 NAME = re.compile(<str literal>, [flags]) 的 AST 事实。"""
    out = {}
    for n in ast.parse(source(rel)).body:
        if not isinstance(n, ast.Assign) or not isinstance(n.value, ast.Call):
            continue
        fn = n.value.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "compile"
                and isinstance(fn.value, ast.Name) and fn.value.id == "re"):
            continue
        if not n.value.args or not isinstance(n.value.args[0], ast.Constant):
            continue
        flags = 0
        if len(n.value.args) > 1 and isinstance(n.value.args[1], ast.Attribute):
            flags = getattr(re, n.value.args[1].attr, -1)
        for t in n.targets:
            if isinstance(t, ast.Name):
                out[t.id] = (n.value.args[0].value, flags)
    return out


for spec in SPECS:
    rel, cid = spec["file"], spec["id"]
    const = CONST_BY_ID[cid]
    consts = compile_consts(rel)
    ok = (const in consts
          and consts[const][0] == spec["pattern"]
          and consts[const][1] == flag_value(spec["flags"]))
    check("常量 %-26s pattern/flags 与 golden 一致" % const, ok,
          "found=%r want_flags=%d" % (consts.get(const), flag_value(spec["flags"])))

    tree = ast.parse(source(rel))
    calls = [c for c in ast.walk(tree)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
             and c.func.attr == spec["op"]
             and isinstance(c.func.value, ast.Name) and c.func.value.id == const]
    check("常量 %-26s 在函数体内被真实调用" % const, len(calls) >= 1,
          "calls=%d" % len(calls))

# 循环体内不再有 re.sub/search/match/findall("字面量") —— 优化成果的硬门槛
RE_OPS = {"sub", "subn", "search", "match", "findall", "fullmatch", "split", "finditer", "compile"}
leftovers = []
for rel in sorted({s["file"] for s in SPECS}):
    for n in ast.walk(ast.parse(source(rel))):
        if not isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
            continue
        for sub in ast.walk(n):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in RE_OPS
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "re" and sub.args
                    and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)):
                leftovers.append((rel, n.lineno, sub.func.attr))
check("7 个文件的循环体内已无字面量正则调用（16 → 0）",
      not leftovers, str(leftovers[:3]))

# 全局：app/ 下所有循环内的字面量正则也应为 0（治理门槛的可执行版本）
all_leftovers = []
for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "app")):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for name in filenames:
        if not name.endswith(".py"):
            continue
        path = os.path.join(dirpath, name)
        try:
            tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
                continue
            for sub in ast.walk(n):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in RE_OPS
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == "re" and sub.args
                        and isinstance(sub.args[0], ast.Constant)
                        and isinstance(sub.args[0].value, str)):
                    all_leftovers.append(os.path.relpath(path, ROOT).replace(os.sep, "/"))
check("app/** 全库循环内字面量正则调用为 0",
      not all_leftovers, str(sorted(set(all_leftovers))[:3]))

# 同一文件里循环外的同 pattern 重复调用也一并收敛到常量（16 循环内 + 5 循环外 = 21）
# 只检查与 golden 同 pattern 的调用；文件里 unrelated 的正则调用保留原样，不属于本批范围。
golden_patterns = {s["pattern"] for s in SPECS}
leftover_dup_calls = []
const_call_total = 0
for rel in sorted({s["file"] for s in SPECS}):
    tree = ast.parse(source(rel))
    own_consts = set(compile_consts(rel))
    for c in ast.walk(tree):
        if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and isinstance(c.func.value, ast.Name)):
            continue
        if (c.func.attr in RE_OPS and c.func.attr != "compile"
                and c.func.value.id == "re" and c.args
                and isinstance(c.args[0], ast.Constant)
                and isinstance(c.args[0].value, str)
                and c.args[0].value in golden_patterns):
            leftover_dup_calls.append((rel, c.lineno, c.func.attr))
        if (c.func.value.id in own_consts
                and c.func.attr in ("sub", "search", "match", "findall")):
            const_call_total += 1
check("7 个文件内与 golden 同 pattern 的字面量重复调用已消失",
      not leftover_dup_calls, str(leftover_dup_calls[:3]))
check("21 个调用点全部走 16 个模块级预编译常量",
      const_call_total == 21, str(const_call_total))

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("LOOP REGEX EQUIVALENCE CHECKS PASSED")
