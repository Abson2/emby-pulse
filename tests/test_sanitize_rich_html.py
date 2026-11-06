#!/usr/bin/env python3
"""回归测试：富文本净化（sanitize_rich_html）与历史二次转义兼容（decode_announcement_content）。

背景：早期 sanitize_rich_html 依赖 bleach，但 bleach 未列入依赖，ImportError 退化为
html.escape，把 <p>...</p> 整体转义成 &lt;p&gt;...&lt;/p&gt; 存入数据库，导致公告
在前端露出 &lt;p&gt; 字面量（公告内容乱码）。

本测试断言：
  1. 富文本安全标签被保留（不再二次转义）；
  2. 危险标签 / 属性 / 协议被剥离；
  3. 历史二次转义数据可被 decode_announcement_content 还原，真实富文本不受影响。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.security_utils import decode_announcement_content, sanitize_rich_html

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


# 1. 富文本安全标签被保留，不再二次转义
rich = '<p>欢迎大家</p>'
out = sanitize_rich_html(rich)
check("基础段落被保留（不二次转义）", out == rich, out)
check("输出不含实体化的 &lt;p&gt;", "&lt;p&gt;" not in out, out)

out2 = sanitize_rich_html('<p><strong>粗体</strong> 与 <em>斜体</em> 和 <u>下划线</u> 和 <s>删除线</s></p>')
check("strong/em/u/s 被保留", all(t in out2 for t in ("<strong>", "<em>", "<u>", "<s>")), out2)

out3 = sanitize_rich_html('<span style="color:#ff0000">红</span> <a href="https://e.com" target="_blank">链接</a>')
check("span style 被保留", 'style="color:#ff0000"' in out3, out3)
check("a href/target 被保留", 'href="https://e.com"' in out3 and 'target="_blank"' in out3, out3)

out4 = sanitize_rich_html("<ul><li>甲</li><li>乙</li></ul><h2>标题</h2><br>")
check("ul/li/h2/br 被保留", all(t in out4 for t in ("<ul>", "<li>", "<h2>", "<br>")), out4)

# 2. 危险内容被剥离
out5 = sanitize_rich_html("<script>alert(1)</script><p>安全</p>")
check("script 标签被移除（仅保留文本）", "<script" not in out5 and "安全" in out5, out5)

out6 = sanitize_rich_html('<img src="x" onerror="alert(1)">')
check("img 危险属性 onerror 被剥离", "onerror" not in out6, out6)

out7 = sanitize_rich_html('<a href="javascript:alert(1)">坏</a><p>好</p>')
check("javascript: 协议 href 被剥离", "javascript:" not in out7, out7)

out8 = sanitize_rich_html("<p><unknown>内容</unknown></p>")
check("未知标签被剥离但保留文本", "<unknown" not in out8 and "内容" in out8, out8)

# 3. 文本中的特殊字符被转义（防 XSS 文本注入）
out9 = sanitize_rich_html("2 < 3 & 4")
check("纯文本中的 < 与 & 被转义", "&lt;" in out9 and "&amp;" in out9, out9)

# 4. 历史二次转义数据兼容
leg = "&lt;p&gt;欢迎大家&lt;/p&gt;"
check("decode 还原历史二次转义", decode_announcement_content(leg) == "<p>欢迎大家</p>", decode_announcement_content(leg))
check("真实富文本 decode 不破坏", decode_announcement_content("<p>欢迎大家</p>") == "<p>欢迎大家</p>")
check("无标签纯文本 decode 不变", decode_announcement_content("纯文本") == "纯文本")

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("SANITIZE RICH HTML CHECKS PASSED")
