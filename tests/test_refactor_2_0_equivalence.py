#!/usr/bin/env python3
"""2.0 重构等价性 / 安全加固回归（纯标准库，可直接 python 运行，无需 pytest / fastapi）。

背景：2.0 首批【低风险清理】改动了插件日志写入、公告表 bootstrap、公告解码循环、
用户端富文本渲染与日志脱敏。硬约束是「100% 保留功能、输入输出行为不变、接口不变」。
本测试用三类手段锁定等价性：

  1. **行为对比**：内存 SQLite 双库并行执行「旧实现」与「新实现」，逐行比对最终表内容
     （含行数上限、留存清理、单条截断、多插件隔离、边界 N=1/2/7/500）。
  2. **AST 抽取执行**：把目标函数从源码中抽出独立 exec，桩掉外部依赖后验证真实行为
     （_decode_rows_content / check_webhook_token / TABLE_INDEXES）。
  3. **静态契约**：锁定对外接口与前端净化接线，防止重构中悄悄删特性。

用法: python tests/test_refactor_2_0_equivalence.py [ROOT]
"""
from __future__ import annotations

import ast
import contextlib
import datetime as _dt
import importlib.util
import os
import re
import secrets
import sqlite3
import sys
import types

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
PASSED = 0
FAILS: list[str] = []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  " + name)
    else:
        FAILS.append(name + ("  " + detail if detail else ""))
        print("  FAIL  " + name + ("  " + detail if detail else ""))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def load_module(name, path, stubs=None):
    """从文件加载模块，允许先向 sys.modules 塞桩，避免拉起完整应用依赖。"""
    if stubs:
        for mod_name, attrs in stubs.items():
            m = sys.modules.get(mod_name) or types.ModuleType(mod_name)
            for k, v in attrs.items():
                setattr(m, k, v)
            sys.modules[mod_name] = m
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def extract_function(src, fn_name, extra_ns=None):
    """AST 抽取单个顶层函数并独立 exec，返回可调用对象。"""
    tree = ast.parse(src)
    node = next(n for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name)
    ns = dict(extra_ns or {})
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<extract>", "exec"), ns)
    return ns[fn_name]


# ---------------------------------------------------------------------------
# 桩：SystemStore（复用单连接；connect/execute 计数用于验证事务合并）
# ---------------------------------------------------------------------------
class FakeStore:
    def __init__(self):
        self.db_path = ":memory:"
        self.conn = sqlite3.connect(":memory:")
        self.connect_calls = 0
        self.execute_calls = 0
        self.conn.execute(
            "CREATE TABLE plugin_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " plugin_id TEXT, level TEXT, message TEXT, created_at DATETIME)"
        )
        self.conn.execute("CREATE TABLE announcements (id INTEGER PRIMARY KEY)")
        self.conn.execute("CREATE TABLE announcement_reads (id INTEGER PRIMARY KEY)")
        self.conn.commit()

    @contextlib.contextmanager
    def connect(self, timeout=20.0):
        self.connect_calls += 1
        yield self.conn

    def execute(self, sql, params=()):
        # 真实 SystemStore.execute 每条 SQL 都会 connect() 一次
        self.connect_calls += 1
        self.execute_calls += 1
        cur = self.conn.cursor()
        cur.execute(sql, params)
        self.conn.commit()
        return cur.rowcount


class FixedDateTime:
    @classmethod
    def now(cls):
        return _dt.datetime(2026, 11, 6, 21, 0, 0)


def install_app_stub_packages():
    for name in ("app", "app.infra", "app.infra.db", "app.core"):
        if name not in sys.modules:
            m = types.ModuleType(name)
            m.__path__ = []
            sys.modules[name] = m


print("== 1. 插件日志：新旧实现逐行等价（A1/A2） ==")
install_app_stub_packages()

# 2.0 B3：message_dao 的缓存门控改用通用 EnsureOnce，测试环境注册真实实现
_ensure_once_mod = load_module(
    "app.infra.db.ensure_once",
    os.path.join(ROOT, "app/infra/db/ensure_once.py"))
sys.modules["app.infra.db"].ensure_once = _ensure_once_mod

ddl_calls: list[str] = []
store_new = FakeStore()
sys.modules["app.infra.db.schema_bootstrap"] = types.ModuleType("app.infra.db.schema_bootstrap")
sys.modules["app.infra.db.schema_bootstrap"].ensure_registered_table = (
    lambda cursor, table_name: ddl_calls.append(table_name)
)
sys.modules["app.infra.db.system_store"] = types.ModuleType("app.infra.db.system_store")
sys.modules["app.infra.db.system_store"].system_store = store_new
plugin_dao = load_module("ep_plugin_dao_2_0", os.path.join(ROOT, "app/plugins/plugin_dao.py"))
plugin_dao.datetime = FixedDateTime

FIXED_NOW = FixedDateTime.now()
CREATED_AT = FIXED_NOW.strftime("%Y-%m-%d %H:%M:%S")
RETENTION_BEFORE = (FIXED_NOW - _dt.timedelta(days=plugin_dao.PLUGIN_LOG_RETENTION_DAYS)).strftime(
    "%Y-%m-%d %H:%M:%S"
)


def legacy_add_plugin_log(store, plugin_id, level, message, max_rows):
    """1.9.10 原实现（逐条 execute，NOT IN 裁剪），逐字复刻作为对照基线。"""
    message = (message or "")[:plugin_dao.PLUGIN_LOG_MESSAGE_MAX]
    store.execute(
        "INSERT INTO plugin_logs (plugin_id, level, message, created_at) VALUES (?, ?, ?, ?)",
        (plugin_id, level, message, CREATED_AT),
    )
    store.execute(
        "DELETE FROM plugin_logs WHERE plugin_id = ?"
        " AND id NOT IN ("
        "   SELECT id FROM plugin_logs WHERE plugin_id = ?"
        "   ORDER BY id DESC LIMIT ?)",
        (plugin_id, plugin_id, max_rows),
    )
    store.execute(
        "DELETE FROM plugin_logs WHERE plugin_id = ? AND created_at < ?",
        (plugin_id, RETENTION_BEFORE),
    )


def snapshot(store):
    return [tuple(r) for r in store.conn.execute(
        "SELECT id, plugin_id, level, message, created_at FROM plugin_logs ORDER BY id"
    ).fetchall()]


def run_equivalence_case(case_name, sequence, max_rows, preload_stale=False):
    """双库并行跑同一序列，逐行比对最终表内容。"""
    old = FakeStore()
    new = FakeStore()
    plugin_dao.system_store = new
    plugin_dao.PLUGIN_LOG_MAX_ROWS = max_rows

    if preload_stale:
        for s in (old, new):
            s.conn.execute(
                "INSERT INTO plugin_logs (plugin_id, level, message, created_at) VALUES (?,?,?,?)",
                ("A", "info", "stale-history", "2000-01-01 00:00:00"),
            )
            s.conn.commit()

    for pid, msg in sequence:
        legacy_add_plugin_log(old, pid, "info", msg, max_rows)
        plugin_dao.add_plugin_log(pid, "info", msg)

    old_rows, new_rows = snapshot(old), snapshot(new)
    check("等价：" + case_name, old_rows == new_rows,
          "old=%d new=%d | old_tail=%s new_tail=%s" % (
              len(old_rows), len(new_rows), old_rows[-2:], new_rows[-2:]))
    return old, new


# 边界场景：N=1 / 2 / 7 / 500，序列长度均超过上限，且含超长消息与多插件
for n in (1, 2, 7, 500):
    seq = [("A", "msg-%d" % i) for i in range(n + 3)]
    run_equivalence_case("行数上限 N=%d（插入 %d 条）" % (n, n + 3), seq, n)

# 多插件隔离 + 历史超期记录 + 单条截断
mixed = []
for i in range(9):
    mixed.append(("A", "A-%d" % i))
    mixed.append(("B", "B-%d" % i))
mixed.append(("A", "X" * 2500))       # 触发 2000 字截断
mixed.append(("B", ""))                # 空消息
run_equivalence_case("多插件隔离 / 留存清理 / 单条截断 / 空消息", mixed, 5, preload_stale=True)

# 截断语义与连接次数（性能断言）
store_equiv = FakeStore()
plugin_dao.system_store = store_equiv
plugin_dao.PLUGIN_LOG_MAX_ROWS = 500
long_msg = "L" * 2500
plugin_dao.add_plugin_log("A", "info", long_msg)
row = store_equiv.conn.execute("SELECT message FROM plugin_logs ORDER BY id DESC LIMIT 1").fetchone()
check("单条消息截断到 2000 字符", row[0] == "L" * 2000, "len=%d" % len(row[0]))
check("新实现仅 1 次连接（原实现 3 次）", store_equiv.connect_calls == 1,
      "connect_calls=%d" % store_equiv.connect_calls)
check("新实现不再逐条调用 execute", store_equiv.execute_calls == 0,
      "execute_calls=%d" % store_equiv.execute_calls)
check("旧实现确为 3 次连接（基线自证）",
      (lambda s: (legacy_add_plugin_log(s, "A", "info", "x", 500), s.connect_calls)[1])(FakeStore()) == 3)

# 常量与公开接口未变
check("运行期常量保持 30 天留存 / 2000 字截断",
      plugin_dao.PLUGIN_LOG_RETENTION_DAYS == 30
      and plugin_dao.PLUGIN_LOG_MESSAGE_MAX == 2000)
dao_src = read("app/plugins/plugin_dao.py")
check("源码常量仍为 500 / 30 / 2000",
      all(x in dao_src for x in ("PLUGIN_LOG_MAX_ROWS = 500", "PLUGIN_LOG_RETENTION_DAYS = 30",
                                "PLUGIN_LOG_MESSAGE_MAX = 2000")))
check("plugin_dao 公开函数集合不变",
      all(("def %s(" % fn) in dao_src for fn in (
          "ensure_plugin_tables", "list_plugin_states", "set_plugin_enabled",
          "get_plugin_config", "save_plugin_config", "add_plugin_log",
          "list_plugin_logs", "clear_plugin_logs")))

print("== 2. 索引增补（A3，纯加法） ==")
reg_src = read("app/infra/db/schema_registry.py")
tree = ast.parse(reg_src)
idx_assign = next(n for n in tree.body
                 if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "TABLE_INDEXES" for t in n.targets))
ns = {}
exec(compile(ast.Module(body=[idx_assign], type_ignores=[]), "<idx>", "exec"), ns)
plugin_indexes = ns["TABLE_INDEXES"].get("plugin_logs", [])
check("新增 idx_plugin_logs_created_at",
      any("idx_plugin_logs_created_at" in s for s in plugin_indexes), str(plugin_indexes))
check("原 idx_plugin_logs_plugin_id 仍在（不删特性）",
      any("idx_plugin_logs_plugin_id" in s for s in plugin_indexes), str(plugin_indexes))
check("全部为 IF NOT EXISTS（幂等、不改结构）",
      all("IF NOT EXISTS" in s for s in plugin_indexes), str(plugin_indexes))

print("== 3. 公告表 bootstrap 一次性缓存（A4） ==")
store_ddl = FakeStore()
ddl_calls.clear()
sys.modules["app.infra.db.schema_bootstrap"].ensure_registered_table = (
    lambda cursor, table_name: ddl_calls.append(table_name)
)
sys.modules["app.infra.db.system_store"].system_store = store_ddl
message_dao = load_module("ep_message_dao_2_0", os.path.join(ROOT, "app/domains/notifications/message_dao.py"))

message_dao.ensure_announcement_tables()
message_dao.ensure_announcement_tables()
message_dao.ensure_announcement_tables()
check("连续 3 次只执行 1 次 DDL 连接", store_ddl.connect_calls == 1,
      "connect_calls=%d" % store_ddl.connect_calls)
check("两张表均被 bootstrap", ddl_calls.count("announcements") == 1
      and ddl_calls.count("announcement_reads") == 1, str(ddl_calls))

store_ddl.db_path = "/tmp/another.db"
message_dao.ensure_announcement_tables()
check("换数据库路径自动失效并重新执行", store_ddl.connect_calls == 2,
      "connect_calls=%d" % store_ddl.connect_calls)

message_dao.reset_announcement_tables_cache()
message_dao.ensure_announcement_tables()
check("显式 reset 后重新执行（运维/测试可控）", store_ddl.connect_calls == 3,
      "connect_calls=%d" % store_ddl.connect_calls)
check("reset 钩子不删除任何原有函数",
      all(("def %s(" % fn) in read("app/domains/notifications/message_dao.py") for fn in (
          "ensure_msg_tables", "ensure_announcement_tables", "ensure_mute_table",
          "list_announcements", "create_announcement", "update_announcement_fields")))

# DDL 失败时不写缓存，下次重试
store_fail = FakeStore()
sys.modules["app.infra.db.system_store"].system_store = store_fail
fail_state = {"n": 0}


def flaky_ddl(cursor, table_name):
    fail_state["n"] += 1
    if table_name == "announcements" and fail_state["n"] == 1:
        raise RuntimeError("simulated ddl failure")


sys.modules["app.infra.db.schema_bootstrap"].ensure_registered_table = flaky_ddl
message_dao2 = load_module("ep_message_dao_2_0_retry", os.path.join(ROOT, "app/domains/notifications/message_dao.py"))
try:
    message_dao2.ensure_announcement_tables()
except RuntimeError:
    pass
message_dao2.ensure_announcement_tables()
check("DDL 异常不写缓存，下次调用会重试", store_fail.connect_calls == 2,
      "connect_calls=%d" % store_fail.connect_calls)

print("== 4. 公告解码 helper（A5）与接口不变 ==")
sec_utils = load_module("ep_security_utils_2_0", os.path.join(ROOT, "app/core/security_utils.py"))
router_src = read("app/domains/notifications/announcements_router.py")
decode_rows = extract_function(
    router_src, "_decode_rows_content",
    {"decode_announcement_content": sec_utils.decode_announcement_content},
)
rows = [
    {"id": 1, "title": "正常", "content": "<p>欢迎</p>", "is_active": 1},
    {"id": 2, "title": "历史", "content": "&lt;p&gt;欢迎大家&lt;/p&gt;"},
    {"id": 3, "title": "无内容"},
    {"id": 4, "title": "纯文本", "content": "纯文本"},
]
before = [dict(r) for r in rows]
ret = decode_rows(rows)
check("历史二次转义数据被还原", rows[1]["content"] == "<p>欢迎大家</p>", str(rows[1]))
check("真实富文本不被破坏", rows[0]["content"] == "<p>欢迎</p>", str(rows[0]))
check("缺 content 的行补空串且不新增其它字段",
      rows[2]["content"] == "" and set(rows[2]) == {"id", "title", "content"}, str(rows[2]))
check("纯文本不变", rows[3]["content"] == "纯文本")
check("helper 原地改写、返回 None、列表长度/顺序不变",
      ret is None and len(rows) == 4 and [r["id"] for r in rows] == [1, 2, 3, 4]
      and [r["title"] for r in rows] == [b["title"] for b in before])

router_tree = ast.parse(router_src)
router_fns = {
    n.name: n for n in router_tree.body
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
}


def _calls_decode_helper(node):
    return any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
               and x.func.id == "_decode_rows_content" for x in ast.walk(node))


def _has_for_loop(node):
    return any(isinstance(x, ast.For) for x in ast.walk(node))


check("两个 GET 接口都调用 helper（消除重复循环）",
      _calls_decode_helper(router_fns["get_announcements"])
      and _calls_decode_helper(router_fns["user_get_announcements"]))
check("helper 内部仍保留解码循环（逻辑未丢）",
      _has_for_loop(router_fns["_decode_rows_content"]))
check("两个 GET 接口内不再残留重复解码循环",
      not _has_for_loop(router_fns["get_announcements"])
      and not _has_for_loop(router_fns["user_get_announcements"]))
check("对外 API 路由集合不变（7 条）",
      all(p in router_src for p in (
          '"/api/announcements"', '"/api/announcements/{ann_id}"',
          '"/api/announcements/{ann_id}/view"', '"/api/user/announcements"',
          '"/api/user/announcements/{ann_id}/read"')))

print("== 5. 用户端富文本净化（S1'） ==")
js_src = read("static/js/request_app.js")
req_tpl = read("templates/request.html")
explore_tpl = read("templates/request/tab_explore.html")
check("request_app.js 定义全局 sanitizeRichText",
      "window.sanitizeRichText = function(" in js_src)
check("DOMPurify 不可用时降级返回原值（功能不破）",
      "typeof DOMPurify === 'undefined'" in js_src and "return html;" in js_src)
check("sanitize 异常也降级返回原值", "catch (e)" in js_src and js_src.count("return html;") >= 2)
check("使用 DOMPurify 默认白名单（不过滤合法 class/style）",
      "DOMPurify.sanitize(html)" in js_src)
check("request.html 引入 DOMPurify 且带 CSP nonce",
      'src="https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js"' in req_tpl
      and re.search(r'<script[^>]*nonce="\{\{ request\.state\.csp_nonce \}\}"[^>]*dompurify', req_tpl) is not None)
check("welcomeModal.message 经净化后渲染",
      'x-html="sanitizeRichText(welcomeModal.message)"' in req_tpl)
check("公告详情经 parseMarkdown + 净化后渲染",
      'x-html="sanitizeRichText(parseMarkdown(announcementDetail.data?.content))"' in explore_tpl)
check("原 parseMarkdown / getAnnouncementPreview 未删除",
      "parseMarkdown(text)" in js_src and "getAnnouncementPreview(content)" in js_src)

print("== 6. 用户级公告 Bark 推送 &lt;p&gt; 实体乱码修复 ==")
to_plain_text = sec_utils.to_plain_text
sanitize_html = sec_utils.sanitize_html

# 6.1 普通纯文本（不含 HTML 特殊字符）输入输出与原实现逐字一致
for plain in ("测试推送", "纯文本无标签 123", "欢迎使用 EmbyPulse 2026", ""):
    check("普通纯文本输入输出不变：%r" % plain[:12],
          to_plain_text(plain, max_length=2000) == sanitize_html(plain, max_length=2000),
          "new=%r old=%r" % (to_plain_text(plain, max_length=2000),
                             sanitize_html(plain, max_length=2000)))

# 含 & / < / > 的纯文本：旧实现错误地实体化，在纯文本 Bark 通道同样是乱码，
# 属于本次修复覆盖范围（新实现原样保留，Web 端存储链路不受影响）。
for special in ("欢迎使用 A & B", "2 < 3 的比较"):
    out = to_plain_text(special, max_length=2000)
    check("特殊字符纯文本不再产生实体乱码：%r" % special[:12],
          out == special and "&amp;" not in out and "&lt;" not in out, repr(out))
    legacy = sanitize_html(special, max_length=2000)
    check("对照：旧实现确实产生实体乱码", legacy != special and ("&amp;" in legacy or "&lt;" in legacy),
          repr(legacy))

# 6.2 富文本输入：不再出现实体/标签字面量
buggy_legacy = sanitize_html("<p>测试推送</p>", max_length=2000)
check("复现旧缺陷：sanitize_html 产出 &lt;p&gt; 实体",
      "&lt;p&gt;" in buggy_legacy, buggy_legacy)
for rich in (
    "<p>测试推送</p>",
    "&lt;p&gt;测试推送&lt;/p&gt;",                    # 历史二次转义数据
    "<p><strong>加粗</strong> 与 <em>斜体</em></p>",
    "<p>第一行</p><p>第二行</p>",
    "<b>新入库 电影 英雄连</b> (2020)",
):
    out = to_plain_text(rich, max_length=2000)
    check("富文本转纯文本无标签/实体：%s" % rich[:18],
          bool(out.strip()) and "<" not in out and ">" not in out
          and "&lt;" not in out and "&gt;" not in out and "<p" not in out, out)

check("具体 bug 样例：<p>测试推送</p> → 测试推送",
      to_plain_text("<p>测试推送</p>", max_length=2000) == "测试推送",
      repr(to_plain_text("<p>测试推送</p>", max_length=2000)))
check("历史实体样例：&lt;p&gt;测试推送&lt;/p&gt; → 测试推送",
      to_plain_text("&lt;p&gt;测试推送&lt;/p&gt;", max_length=2000) == "测试推送",
      repr(to_plain_text("&lt;p&gt;测试推送&lt;/p&gt;", max_length=2000)))
check("保留标签内文字（剥离而非丢弃内容）",
      "加粗" in to_plain_text("<p><strong>加粗</strong> 与 <em>斜体</em></p>"))
check("危险模式仍被清理（与 sanitize_html 一致）",
      "javascript:" not in to_plain_text("javascript:alert(1) <p>x</p>"))
check("长度上限仍生效",
      len(to_plain_text("X" * 3000, max_length=2000)) <= 2000)

# 6.3 端到端：转换后的正文再经 Bark 出口 _strip_html 仍无实体
bark_src = read("app/domains/notifications/bark_push.py")
bark_tree = ast.parse(bark_src)
tag_re_assign = next(
    n for n in bark_tree.body
    if isinstance(n, ast.Assign)
    and any(isinstance(t, ast.Name) and t.id == "_HTML_TAG_RE" for t in n.targets)
)
strip_fn = next(n for n in bark_tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_strip_html")
bark_ns = {"str": str, "re": re}
exec(compile(ast.Module(body=[tag_re_assign, strip_fn], type_ignores=[]), "<bark-strip>", "exec"), bark_ns)
strip_html = bark_ns["_strip_html"]
final_body = strip_html(to_plain_text("<p>测试推送</p>", max_length=2000))
check("端到端：Bark 出口后正文为「测试推送」，无 &lt;p&gt;",
      final_body == "测试推送" and "&lt;" not in final_body and "<p" not in final_body,
      repr(final_body))
check("对照：旧路径经 Bark 出口仍残留实体（证明修复有效）",
      "&lt;p&gt;" in strip_html(buggy_legacy))

# 6.4 发布点确实改用 to_plain_text，且网页端富文本链路完全未动
create_fn = router_fns["create_announcement"]
create_src = ast.get_source_segment(router_src, create_fn) or ""
notify_call = next(
    x for x in ast.walk(create_fn)
    if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
    and x.func.attr == "notify_users_by_flag"
)
notify_src = ast.get_source_segment(router_src, notify_call) or ""
check("用户级通知正文改用 to_plain_text",
      "to_plain_text(data.content, max_length=2000)" in notify_src)
check("用户级通知标题改用 to_plain_text(500) 且不再转义",
      "to_plain_text(data.title, max_length=500)" in notify_src
      and "sanitize_html(" not in notify_src)
check("网页端标题存储仍用 sanitize_html（显示链路完全未动）",
      "sanitize_html(data.title)" in create_src)
check("网页端正文存储仍用 sanitize_rich_html（保留富文本，显示不变）",
      "sanitize_rich_html(data.content, max_length=50000)" in create_src)
check("对外字段名 / flag key 不变",
      '"notify_announcement"' in notify_src)

print("== 7. 日志脱敏（S2'） ==")
sec_src = read("app/core/security_check.py")
check("不再输出 Token 前 8 / 后 8 字符片段",
      "new_token[:8]" not in sec_src and "new_token[-8:]" not in sec_src)


class CapturingLogger:
    def __init__(self):
        self.lines = []

    def warning(self, msg, *args):
        self.lines.append(msg % args if args else msg)


captured_tokens = []
logger = CapturingLogger()
check_token_fn = extract_function(
    sec_src, "check_webhook_token",
    {
        "get_webhook_token": lambda: "embypulse",
        "set_webhook_token": lambda t: captured_tokens.append(t),
        "secrets": secrets,
        "logger": logger,
    },
)
result = check_token_fn()
token = captured_tokens[0]
joined = "\n".join(logger.lines)
check("默认 Token 仍会自动生成并保存（功能不变）",
      result is False and len(token) >= 24, "token_len=%d" % len(token))
check("日志不含完整 Token / 前 8 / 后 8 片段",
      token not in joined and token[:8] not in joined and token[-8:] not in joined,
      joined)
check("提示引导仍在", any("Emby Webhook" in line for line in logger.lines))

cfg_src = read("app/core/config.py")
check("config 启动日志不再打印 emby_host 明文",
      "emby_host: {self.config.get('emby_host', '未设置')}" not in cfg_src
      and "emby_host: {'已设置' if self.config.get('emby_host') else '未设置'}" in cfg_src)
check("SENSITIVE_ENV_FIELDS 未被削弱（BARK / WEBHOOK 仍在）",
      '"bark_device_key": "BARK_DEVICE_KEY"' in cfg_src
      and '"webhook_token": "WEBHOOK_TOKEN"' in cfg_src)

print("== 8. 第二批：管理员消息同类乱码收尾 + 零风险边界 ==")
msg_src = read("app/domains/notifications/messages.py")
msg_tree = ast.parse(msg_src)
push_call = next(
    x for x in ast.walk(msg_tree)
    if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
    and x.func.attr == "push_user_notify"
)
push_src = ast.get_source_segment(msg_src, push_call) or ""
check("管理员消息用户级通知正文改用 to_plain_text",
      "to_plain_text(data.content, max_length=5000)" in push_src)
check("该调用内不再使用 sanitize_html 转义", "sanitize_html(" not in push_src)
check("对外参数与 flag key 不变",
      'data.user_id' in push_src and '"管理员消息"' in push_src
      and 'flag_key="notify_admin_message"' in push_src)
check("网页端消息存储仍用 sanitize_html（显示链路零改动）",
      msg_src.count("insert_admin_message(") >= 1
      and msg_src.count("sanitize_html(data.content, max_length=5000)") >= 2)
check("导入 to_plain_text 未删任何原有导入",
      "from app.core.security_utils import safe_error_message, sanitize_html, to_plain_text" in msg_src)

# 行为：管理员消息典型内容不再产生实体乱码
for content in ("您的账号已开通", "欢迎 A & B 使用", "密码 < 8 位不允许", "<b>加粗通知</b>"):
    out = to_plain_text(content, max_length=5000)
    check("管理员消息纯文本通道无实体：%r" % content[:12],
          "&lt;" not in out and "&gt;" not in out and "&amp;" not in out, repr(out))
check("普通管理员消息逐字不变",
      to_plain_text("您的账号已开通", max_length=5000)
      == sanitize_html("您的账号已开通", max_length=5000))

# 未受影响的其它用户级通知入口（本批刻意不动，保持行为）
for rel, expect in (
    ("app/bot/notification_bot/notification_bot_library_new_item_service.py", "notify_users_by_flag"),
    ("app/bot/notification_bot/notification_bot_library_new_episode_service.py", "notify_users_by_flag"),
    ("app/domains/media_requests/management_router.py", "notify_request_users"),
):
    s = read(rel)
    check("未改动入口仍在：%s" % rel.split("/")[-1],
          expect in s and "to_plain_text(" not in s and "sanitize_html(" not in s)

check("用户级通知服务签名未变（对外接口）",
      all(sig in read("app/domains/users/user_notify_service.py") for sig in (
          "def push_user_notify(user_id: str, title: str, body: str, flag_key: str = None) -> dict:",
          "def notify_users_by_flag(flag_key: str, title: str, body: str):",
          "def notify_request_users(tmdb_id, season, flag_key: str, title: str, body: str):")))

# 安全契约脚本：纯加法、只读源码、无第三方依赖
# 注意：不能用裸子串判断「没有 import bleach / pytest」——契约脚本自身就包含
# 这类字符串用于检查 security_utils.py，会被自己的文本误伤。改用 AST 只检查
# 真实的 import 语句顶层模块名。
sec_contract = read("tools/check_security_contracts.py")
sec_tree = ast.parse(sec_contract)
sec_imports = set()
for _node in ast.walk(sec_tree):
    if isinstance(_node, ast.Import):
        sec_imports.update(_a.name.split(".")[0] for _a in _node.names)
    elif isinstance(_node, ast.ImportFrom) and _node.module:
        sec_imports.add(_node.module.split(".")[0])
check("存在 2.0 安全契约脚本且只用标准库",
      os.path.isfile(os.path.join(ROOT, "tools/check_security_contracts.py"))
      and sec_imports <= {"__future__", "importlib", "os", "sys"},
      repr(sorted(sec_imports)))
check("安全契约脚本只读源码（不改运行时行为）",
      "open(" in sec_contract and "os.remove" not in sec_contract and "shutil" not in sec_contract)
ci_src = read(".github/workflows/docker-publish.yml")
check("安全契约与富文本净化回归均接入 CI",
      "tools/check_security_contracts.py" in ci_src
      and "tests/test_sanitize_rich_html.py" in ci_src)
check("2.0.1 补丁版本点已同步（v2.0 tag 不重推）",
      'APP_VERSION = "2.0.1"' in read("app/shared/version.py")
      and "IMAGE_TAG:-2.0.1" in read("docker-compose.yml")
      and "request_app.js?v=2.0.1" in read("templates/request.html"))

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("REFACTOR 2.0 EQUIVALENCE CHECKS PASSED")
