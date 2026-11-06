#!/usr/bin/env python3
"""三条净化通道的 golden + 属性回归（2.0 之后「梯队 0」治理能力）。

为什么需要这份测试：B5（写入侧统一净化）与 B2 之所以被硬约束挡住，是因为
三条通道是**刻意分层**而非冗余：

    sanitize_html      网页端消息存储 / 需转义展示   → 整体 html.escape
    to_plain_text      用户级 Bark / Webhook 纯文本  → 反转义 + 剥标签
    sanitize_rich_html 公告 / Quill 富文本网页端     → 白名单保留标签

任何「统一」都会改变其中一路的输出。本测试把三路在 25 组输入上的输出
**在改代码前采集为 golden**，再叠加结构性不变量，把这条边界变成 CI 断言。

基线采集于 2026-11-07（``tests/sanitizer_golden.json``），采集时代码未改动。
属性断言全部按实测行为书写，其中三条是反直觉但必须锁死的事实：

  1. ``sanitize_html`` **不幂等**（含标签输入二次调用会再转义 & → &amp;），
     不能写成「所有净化器都应幂等」；
  2. ``sanitize_rich_html`` **完全幂等**（25/25），可以强断言；
  3. ``sanitize_rich_html`` **保留纯文本节点里的** ``javascript:`` 字面量，
     危险模式清理只发生在标签 / 属性 / style 上下文，不能断言「全文无危险词」。

纯标准库：用 ``importlib.util`` 单文件加载 security_utils，不触发包级 import。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
GOLDEN_PATH = os.path.join(HERE, "sanitizer_golden.json")
SEC_PATH = os.path.join(ROOT, "app", "core", "security_utils.py")

_spec = importlib.util.spec_from_file_location("_prop_security_utils", SEC_PATH)
secu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(secu)

with open(GOLDEN_PATH, encoding="utf-8") as _f:
    GOLDEN = json.load(_f)

# 与基线采集时逐字一致的输入集；若此处漂移，第 1 节 golden 比对会立刻失败。
CASES = {
    "empty": "",
    "plain_ascii": "hello world",
    "plain_cn": "欢迎使用系统",
    "quill_p": "<p>测试推送</p>",
    "quill_multi": '<p class="ql-align-center">欢迎<span style="color: red">大家</span></p>',
    "escaped_quill": "&lt;p&gt;测试推送&lt;/p&gt;",
    "double_escaped": "&amp;lt;p&amp;gt;测试&amp;lt;/p&amp;gt;",
    "bold_tg": "<b>新入库 电影 英雄连</b> (2020)",
    "script": "<script>alert(1)</script>",
    "script_nested": "<p>a<script>alert(1)</script>b</p>",
    "onclick": '<b onclick="alert(1)">x</b>',
    "js_href": '<a href="javascript:alert(1)">click</a>',
    "js_text": "javascript:alert(1)",
    "vbscript": "vbscript:msgbox(1)",
    "data_html": "data:text/html,<script>x</script>",
    "css_url": '<span style="background:url(http://evil/x)">x</span>',
    "css_expr": '<div style="width:expression(alert(1))">x</div>',
    "css_import": '<div style="@import url(x)">x</div>',
    "css_safe": '<div style="color: red; text-align: center">x</div>',
    "amp_entity": "A &amp; B &lt; C &gt; D",
    "unclosed_p": "<p>a",
    "img": '<img src="https://x/y.png" alt="y">',
    "mixed": '<p>Hello <b>world</b><br>next &amp; last</p>',
    "null_byte": "a\x00b",
    "long_text": "x" * 6000,
}

CHANNELS = {
    "sanitize_html": secu.sanitize_html,
    "to_plain_text": secu.to_plain_text,
    "sanitize_rich_html": secu.sanitize_rich_html,
}

ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")
TAG_RE = re.compile(r"<[!/]?[a-zA-Z][^>]*>")
DANGER_RE = re.compile(r"javascript:|vbscript:|data:text/html|on\w+\s*=", re.I)
TAG_CONTEXT_DANGER_RE = re.compile(
    r"<script|on\w+\s*=|javascript:|url\s*\(|expression\s*\(|@import|vbscript:", re.I)

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


def outputs():
    return {ch: {k: fn(v) for k, v in CASES.items()} for ch, fn in CHANNELS.items()}


OUT = outputs()

# ---------------------------------------------------------------- 1. golden
print("== 1. 改代码前采集的 golden 逐字比对 ==")
for ch in CHANNELS:
    expected = GOLDEN[ch]
    missing = sorted(set(CASES) - set(expected))
    diffs = [(k, OUT[ch][k], expected[k]) for k in sorted(set(CASES) & set(expected))
             if OUT[ch][k] != expected[k]]
    check("golden %-20s %d/%d 组逐字一致" % (ch, len(expected) - len(diffs), len(expected)),
          not missing and not diffs,
          ("缺 case: %s" % missing) if missing else
          ("; ".join("%s: got=%r want=%r" % (k, g[:60], w[:60]) for k, g, w in diffs[:3])))
    if missing:
        print("        missing: %s" % missing)
    for k, g, w in diffs[:3]:
        print("        %s: got=%r want=%r" % (k, g[:100], w[:100]))

# --------------------------------------------------- 2. 幂等性（按实测分层）
print("\n== 2. 幂等性：三通道性质不同，不能一刀切 ==")
check("sanitize_rich_html 全部 25 组输入幂等",
      all(secu.sanitize_rich_html(v) == v for v in
          (OUT["sanitize_rich_html"][k] for k in CASES)))

# to_plain_text 对「非多重转义历史数据」幂等；double_escaped 是唯一例外，
# 因为一轮 unescape 只还原一层 &amp;lt; → &lt;。
plain_cases = {k: v for k, v in CASES.items() if k != "double_escaped"}
check("to_plain_text 对 24 组非多重转义输入幂等",
      all(secu.to_plain_text(OUT["to_plain_text"][k]) == OUT["to_plain_text"][k]
          for k in plain_cases),
      str([k for k in plain_cases
           if secu.to_plain_text(OUT["to_plain_text"][k]) != OUT["to_plain_text"][k]]))

# 明确锁定「sanitize_html 不幂等」这一事实：顺手「修成幂等」会改变网页端
# 已有的双重转义数据展示，属于功能变更，必须被拦下。
check("sanitize_html 含标签输入明确不幂等（锁定既有行为）",
      secu.sanitize_html(OUT["sanitize_html"]["quill_p"])
      == GOLDEN["sanitize_html"]["escaped_quill"]
      and secu.sanitize_html(OUT["sanitize_html"]["quill_p"])
      != OUT["sanitize_html"]["quill_p"])
check("sanitize_html 对不含 <>&\"' 的纯文本输入幂等",
      all(secu.sanitize_html(OUT["sanitize_html"][k]) == OUT["sanitize_html"][k]
          for k, v in CASES.items() if not any(c in v for c in "<>&\"'")),
      str([k for k, v in CASES.items() if not any(c in v for c in "<>&\"'")
           and secu.sanitize_html(OUT["sanitize_html"][k]) != OUT["sanitize_html"][k]]))

# --------------------------------------------- 3. 输出结构不变量（防 XSS）
print("\n== 3. 输出结构不变量 ==")
check("sanitize_html 输出永不含真实 HTML 标签",
      all(not TAG_RE.search(v) for v in OUT["sanitize_html"].values()))
check("sanitize_html / to_plain_text 输出不残留危险模式",
      all(not DANGER_RE.search(v)
          for ch in ("sanitize_html", "to_plain_text") for v in OUT[ch].values()))
check("to_plain_text 非多重转义输入输出无标签、无实体（Bark 纯文本通道）",
      all(not TAG_RE.search(OUT["to_plain_text"][k])
          and not ENTITY_RE.search(OUT["to_plain_text"][k]) for k in plain_cases),
      str([k for k in plain_cases
           if TAG_RE.search(OUT["to_plain_text"][k]) or ENTITY_RE.search(OUT["to_plain_text"][k])]))
check("sanitize_rich_html 对含标签输入剥离 script / 事件属性 / 危险协议 / 危险 CSS",
      all(not TAG_CONTEXT_DANGER_RE.search(OUT["sanitize_rich_html"][k])
          for k, v in CASES.items() if "<" in v),
      str([k for k, v in CASES.items() if "<" in v
           and TAG_CONTEXT_DANGER_RE.search(OUT["sanitize_rich_html"][k])]))
check("sanitize_rich_html 保留纯文本节点里的危险字面量（刻意边界，非漏洞）",
      OUT["sanitize_rich_html"]["js_text"] == "javascript:alert(1)"
      and OUT["sanitize_rich_html"]["vbscript"] == "vbscript:msgbox(1)")
check("sanitize_rich_html 合法 style 声明逐字不变（S5 只收紧非法输入）",
      OUT["sanitize_rich_html"]["css_safe"] == CASES["css_safe"])
check("sanitize_rich_html 非白名单标签剥离但文本保留",
      "<unknown" not in secu.sanitize_rich_html("<p><unknown>内容</unknown></p>")
      and "内容" in secu.sanitize_rich_html("<p><unknown>内容</unknown></p>"))
check("sanitize_rich_html 自动补齐未闭合标签",
      OUT["sanitize_rich_html"]["unclosed_p"] == "<p>a</p>")

# ------------------------------------------------------- 4. 截断 / 通道分工
print("\n== 4. 截断上限与三通道分工边界（B5 不可统一的证据）==")
check("截断上限 500 / 2000 生效",
      len(OUT["sanitize_html"]["long_text"]) == 500
      and len(OUT["to_plain_text"]["long_text"]) == 2000
      and len(OUT["sanitize_rich_html"]["long_text"]) == 6000)
check("sanitize_rich_html 上限 50000 生效",
      len(secu.sanitize_rich_html("x" * 60000)) == 50000)
check("纯文本输入三通道输出一致",
      all(len({OUT[ch][k] for ch in CHANNELS}) == 1
          for k in ("empty", "plain_ascii", "plain_cn")))
check("富文本输入三通道输出互不相同（刻意分层）",
      len({OUT[ch]["quill_p"] for ch in CHANNELS}) == 3
      and OUT["sanitize_html"]["quill_p"] == "&lt;p&gt;测试推送&lt;/p&gt;"
      and OUT["to_plain_text"]["quill_p"] == "测试推送"
      and OUT["sanitize_rich_html"]["quill_p"] == "<p>测试推送</p>")
check("Bark 实体乱码根治：历史 &lt;p&gt; 正文转纯文本无实体残留",
      OUT["to_plain_text"]["escaped_quill"] == "测试推送"
      and "&lt;" not in OUT["to_plain_text"]["escaped_quill"]
      and OUT["sanitize_html"]["escaped_quill"] != OUT["to_plain_text"]["escaped_quill"])
check("decode_announcement_content 与富文本通道兼容",
      secu.decode_announcement_content(CASES["escaped_quill"]) == "<p>测试推送</p>"
      and secu.sanitize_rich_html(
          secu.decode_announcement_content(CASES["escaped_quill"])) == "<p>测试推送</p>")

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("SANITIZER PROPERTY CHECKS PASSED")
