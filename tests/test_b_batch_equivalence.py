#!/usr/bin/env python3
"""2.0 批次 B3 / B4 / S5 等价性 + 四项特征契约（纯标准库）。

覆盖：
  * B3 EnsureOnce 门控与旧版缓存逻辑等价（一次执行 / 换 key 失效 / 异常重试 / reset）；
  * B4 SystemStore.execute_many 单连接单事务、失败整批回滚，且 execute 语义不变；
  * S5 富文本 style 只清洗危险 CSS 值，合法声明与合法富文本逐字不变；
  * 契约：配置单例 / 通知链路快照 / 模板契约 / DDL 幂等 / S6 表名拼接防御。

用法: python tests/test_b_batch_equivalence.py [ROOT]
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import os
import sqlite3
import sys
import types

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(os.path.dirname(__file__), ".."))
PASSED = 0
FAILS = []


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


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


print("== 1. B3 EnsureOnce 与旧缓存逻辑等价 ==")
ensure_once = load("ep_ensure_once", "app/infra/db/ensure_once.py")
gate = ensure_once.EnsureOnce()
calls = []


def ddl():
    calls.append(1)


check("同一 key 只执行一次",
      [gate.run("db1", ddl), gate.run("db1", ddl), gate.run("db1", ddl)] == [True, False, False]
      and len(calls) == 1)
check("换 key 自动失效", gate.run("db2", ddl) is True and len(calls) == 2)
gate.reset()
check("reset 后重新执行", gate.run("db1", ddl) is True and len(calls) == 3)

flaky = ensure_once.EnsureOnce()
attempts = []


def broken():
    attempts.append(1)
    if len(attempts) == 1:
        raise RuntimeError("ddl failed")


try:
    flaky.run("db", broken)
except RuntimeError:
    pass
check("异常不写缓存且原样抛出", not flaky.is_ready("db"))
check("异常后下次调用会重试", flaky.run("db", broken) is True and flaky.is_ready("db"))

# message_dao 已接入通用门控，且原有对外符号保留
for name in ("app", "app.infra", "app.infra.db", "app.core"):
    if name not in sys.modules:
        m = types.ModuleType(name)
        m.__path__ = []
        sys.modules[name] = m
sys.modules["app.infra.db.ensure_once"] = ensure_once
sys.modules["app.infra.db"].ensure_once = ensure_once
# 2.0 B3-B：纯建表 helper 为真实模块，注册进桩包供 message_dao 导入
_ddl_bootstrap = load("app.infra.db.ddl_bootstrap", "app/infra/db/ddl_bootstrap.py")
sys.modules["app.infra.db"].ddl_bootstrap = _ddl_bootstrap
bootstrap = types.ModuleType("app.infra.db.schema_bootstrap")
bootstrap.ensure_registered_table = lambda cursor, table: None
sys.modules["app.infra.db.schema_bootstrap"] = bootstrap


class DDLStore:
    def __init__(self):
        self.db_path = "/tmp/a.db"
        self.connect_calls = 0

    @contextlib.contextmanager
    def connect(self):
        self.connect_calls += 1
        cursor = types.SimpleNamespace()
        yield types.SimpleNamespace(cursor=lambda: cursor, commit=lambda: None)


ddl_store = DDLStore()
store_stub = types.ModuleType("app.infra.db.system_store")
store_stub.system_store = ddl_store
sys.modules["app.infra.db.system_store"] = store_stub
mdao = load("ep_message_dao_b3", "app/domains/notifications/message_dao.py")
for _ in range(3):
    mdao.ensure_announcement_tables()
check("message_dao 接入 EnsureOnce 后仍只执行 1 次 DDL", ddl_store.connect_calls == 1,
      str(ddl_store.connect_calls))
ddl_store.db_path = "/tmp/b.db"
mdao.ensure_announcement_tables()
check("换库仍自动失效", ddl_store.connect_calls == 2)
mdao.reset_announcement_tables_cache()
mdao.ensure_announcement_tables()
check("reset 钩子行为不变", ddl_store.connect_calls == 3)
mdao_src = read("app/domains/notifications/message_dao.py")
check("原有模块级状态名与公开函数全部保留",
      all(s in mdao_src for s in ("_announcement_tables_ready", "_announcement_tables_lock",
                                  "def reset_announcement_tables_cache()",
                                  "def ensure_announcement_tables()", "def ensure_msg_tables()")))

print("== 2. B4 execute_many 单事务 / 回滚 / execute 不变 ==")
ss_src = read("app/infra/db/system_store.py")
tree = ast.parse(ss_src)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SystemStore")
fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "execute_many")
ns = {}
exec(compile(ast.Module(body=[fn], type_ignores=[]), "<execute_many>", "exec"), ns)
execute_many = ns["execute_many"]


class MemStore:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE t(x INTEGER)")
        self.conn.commit()
        self.connect_calls = 0

    @contextlib.contextmanager
    def connect(self):
        self.connect_calls += 1
        yield self.conn


store = MemStore()
rc = execute_many(store, [("INSERT INTO t VALUES(?)", (1,)),
                          ("INSERT INTO t VALUES(?)", (2,))])
check("两条语句共用 1 次连接", store.connect_calls == 1, str(store.connect_calls))
check("两条语句均提交且返回末条 rowcount",
      store.conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2 and rc == 1)
store2 = MemStore()
try:
    execute_many(store2, [("INSERT INTO t VALUES(?)", (3,)),
                          ("THIS IS NOT SQL", ())])
except sqlite3.Error:
    pass
check("失败整批回滚，不残留半批数据",
      store2.conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0)
check("execute() 原实现未被替换（仍单条提交）",
      "def execute(self, sql: str, params=()) -> int:" in ss_src
      and "conn.commit()" in ss_src
      and "def fetch_all(" in ss_src and "def connect(" in ss_src)

print("== 3. S5 富文本 style 危险值清洗 ==")
secu = load("ep_security_utils_b3", "app/core/security_utils.py")
plain = '<span style="color:red;text-align:center;font-weight:bold">你好</span>'
check("合法 style 逐字不变", secu.sanitize_rich_html(plain) == plain,
      secu.sanitize_rich_html(plain))
for bad in (
    '<span style="background:url(javascript:alert(1));color:red">x</span>',
    '<div style="width:expression(alert(1))">x</div>',
    '<span style="@import url(http://example.com/a.css)">x</span>',
    '<span style="behavior:url(#default#time2)">x</span>',
):
    out = secu.sanitize_rich_html(bad)
    check("危险 CSS 被移除：%s" % bad[6:26],
          "url(" not in out and "expression(" not in out
          and "@import" not in out and "behavior:" not in out, out)
check("清洗后合法声明仍保留",
      "color:red" in secu.sanitize_rich_html(
          '<span style="background:url(javascript:alert(1));color:red">x</span>'))
check("script / 事件属性 / javascript: 仍被剥离",
      "<script" not in secu.sanitize_rich_html("<script>alert(1)</script><p>OK</p>")
      and "onerror" not in secu.sanitize_rich_html('<img src="x" onerror="alert(1)">')
      and "javascript:" not in secu.sanitize_rich_html('<a href="javascript:alert(1)">x</a>'))
check("公告富文本标签保留（v1.9.10 行为不变）",
      secu.sanitize_rich_html("<p>欢迎大家</p>") == "<p>欢迎大家</p>")

print("== 4. 特征契约：配置 / 通知链路 / 模板 / DDL 幂等 / 表名防御 ==")
cfg = read("app/core/config.py")
check("配置单例契约：关键凭证仍在中央注册表",
      all(k in cfg for k in ('"webhook_token": "WEBHOOK_TOKEN"',
                             '"bark_device_key": "BARK_DEVICE_KEY"',
                             '"tg_bot_token": "TG_BOT_TOKEN"',
                             '"emby_api_key": "EMBY_API_KEY"'))
      and "def get_env_source(" in cfg)
for rel, keys in (
    ("app/bot/notification_bot/notification_bot_library_new_item_service.py",
     ("def handle_library_new_item(bot, item):", '"library_new_item"',
      "notify_users_by_flag", "_bark_push_provider")),
    ("app/bot/notification_bot/notification_bot_library_new_episode_service.py",
     ("def handle_library_new_episode(bot, data):", '"library_new_episode"',
      "notify_users_by_flag", "_bark_push_provider")),
):
    s = read(rel)
    check("通知链路快照：" + rel.split("/")[-1], all(k in s for k in keys))
req_js = read("static/js/request_app.js")
check("模板契约：前端公共净化函数与 Markdown 解析仍在",
      "window.sanitizeRichText = function(" in req_js
      and "parseMarkdown(text) {" in req_js
      and "sanitizeRichText(parseMarkdown(" in read("templates/request/tab_explore.html"))
# B2 前置证据：8 处 escapeHtml 实测为 4 类不同实现，直接统一会改变页面输出。
# 在具备 DOM 级契约测试前，锁定「全部保留、不删除、不替换」。
esc_files = (
    "templates/plugins.html", "templates/tasks.html", "templates/history.html",
    "templates/users.html", "templates/messages.html", "templates/settings.html",
    "static/js/dashboard/utils.js",
)
esc_src = "\n".join(read(p) for p in esc_files)
check("B2 前置：7 个文件共 8 处 escapeHtml 定义全部保留（不删特性）",
      sum(read(p).count("function escapeHtml(") for p in esc_files) == 8)
check("B2 前置：实测存在 4 类行为不同的实现，禁止无 DOM 测试直接统一",
      all(s in esc_src for s in ("div.textContent", "div.innerText",
                                 "&#039;", "&#39;"))
      and "typeof str !== 'string'" in esc_src)

mem = sqlite3.connect(":memory:")
ddl = ("CREATE TABLE IF NOT EXISTS demo(id INTEGER PRIMARY KEY, v TEXT)",
       "CREATE INDEX IF NOT EXISTS idx_demo_v ON demo(v)")
for _ in range(2):
    for sql in ddl:
        mem.execute(sql)
    mem.commit()
schema_a = mem.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
for _ in range(2):
    for sql in ddl:
        mem.execute(sql)
    mem.commit()
schema_b = mem.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
check("DDL 幂等：连续多轮执行表结构完全一致", schema_a == schema_b)
reg = read("app/infra/db/schema_registry.py")
check("S6 表名拼接防御：注册表仍是唯一表结构来源",
      "TABLE_SCHEMAS" in reg and "TABLE_INDEXES" in reg
      and "ensure_registered_table" in read("app/infra/db/schema_bootstrap.py"))

print("== 5. B3-B 纯建表样板抽离（20 处）等价性 ==")


def old_bootstrap(store, ensure_fn, table_names):
    """抽离前逐字复制的 5~6 行骨架。"""
    with store.connect() as conn:
        cursor = conn.cursor()
        for table_name in table_names:
            ensure_fn(cursor, table_name)
        conn.commit()


def make_ddl_store(events):
    class Conn:
        def cursor(self):
            events.append("cursor")
            return self

        def commit(self):
            events.append("commit")

    class Store:
        @contextlib.contextmanager
        def connect(self):
            events.append("connect")
            yield Conn()

    return Store()


for tables in (("a",), ("a", "b", "c"), ()):
    ev_old, ev_new = [], []
    fn_old = lambda c, t, e=ev_old: e.append(("ddl", t))
    fn_new = lambda c, t, e=ev_new: e.append(("ddl", t))
    old_bootstrap(make_ddl_store(ev_old), fn_old, tables)
    _ddl_bootstrap.ensure_registered_tables(make_ddl_store(ev_new), fn_new, tables)
    check("DDL 顺序 / 连接 / cursor / commit 逐事件一致：%r" % (tables,),
          ev_old == ev_new, f"{ev_old} != {ev_new}")

ev_fail_old, ev_fail_new = [], []


def boom(cursor, table):
    raise RuntimeError("ddl failed")


for runner in (
    lambda: old_bootstrap(make_ddl_store(ev_fail_old), boom, ("x",)),
    lambda: _ddl_bootstrap.ensure_registered_tables(
        make_ddl_store(ev_fail_new), boom, ("x",)),
):
    try:
        runner()
    except RuntimeError:
        pass
check("DDL 异常同样向外抛出且不 commit",
      ev_fail_old == ev_fail_new == ["connect", "cursor"])

# 静态契约：20 个调用点已接入，带迁移语义的 ensure_* 一行未动。
# 注意：helper 定义行 `def ensure_registered_tables(` 会让裸子串自匹配成 21 个，
# 因此用 AST 判定「函数体内真实调用」，沿用本项目静态断言一律 AST / 精确正则的纪律。
import pathlib as _pathlib

_callers, _migration = [], []
for _p in sorted(_pathlib.Path(ROOT, "app").rglob("*.py")):
    _text = _p.read_text(encoding="utf-8")
    try:
        _tree = ast.parse(_text)
    except SyntaxError:
        continue
    for _n in _tree.body:
        if isinstance(_n, ast.FunctionDef) and _n.name.startswith("ensure_"):
            _src = ast.get_source_segment(_text, _n) or ""
            _has_call = any(
                isinstance(_c, ast.Call)
                and isinstance(_c.func, ast.Name)
                and _c.func.id == "ensure_registered_tables"
                for _c in ast.walk(_n)
            )
            if _has_call:
                _callers.append((str(_p.relative_to(ROOT)), _n.name))
            if "PRAGMA" in _src.upper() or "ALTER TABLE" in _src.upper():
                _migration.append((str(_p.relative_to(ROOT)), _n.name, _has_call))
check("20 个纯建表样板全部接入公共 helper", len(_callers) == 20, str(len(_callers)))
check("每个调用点函数签名与函数名不变（无参 + -> None）",
      all("def " + name + "() -> None:" in read(p) for p, name in _callers))
check("带 PRAGMA / ALTER 的迁移型 ensure_* 未被改动",
      len(_migration) >= 4
      and all(not called for _, _, called in _migration),
      str([n for _, n, _ in _migration]))
_helper_src = read("app/infra/db/ddl_bootstrap.py")
_helper_fn = next(n for n in ast.parse(_helper_src).body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "ensure_registered_tables")
_helper_body = ast.get_source_segment(_helper_src, _helper_fn) or ""
check("helper 为无状态纯函数（不持有 system_store / 注册表 / 模块级导入）",
      "def ensure_registered_tables(store, ensure_fn, table_names)" in _helper_body
      and "system_store" not in _helper_body
      and "import " not in _helper_body
      and "store.connect()" in _helper_body and "conn.commit()" in _helper_body)

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("B BATCH EQUIVALENCE CHECKS PASSED")
