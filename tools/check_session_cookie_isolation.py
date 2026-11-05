#!/usr/bin/env python3
"""验证 10307/10308 会话 Cookie 隔离（避免两端口互踢）。

只依赖标准库：用桩替换 DB DAO 与 starlette，加载**真实** session.py 与
session_middleware.py 源码，实际执行 DatabaseSessionMiddleware.dispatch，
断言管理端/用户端使用不同 Cookie 名、且各自只读取自己的 Cookie。

用法: python tools/check_session_cookie_isolation.py [ROOT]
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
FAILS: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  " + name)
    else:
        FAILS.append(name + ("  " + detail if detail else ""))
        print("  FAIL  " + name + ("  " + detail if detail else ""))


def _mod(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = m
    return m


# ---------------------------------------------------------------- 桩
# 1) 让真实 session.py 的 DAO import 成功，且 import 期不碰 DB
for _n in ("app", "app.infra", "app.infra.db", "app.infra.db.session_dao", "app.core"):
    _mod(_n)
_dao = sys.modules["app.infra.db.session_dao"]
for _fname in ("cleanup_expired_sessions", "create_session", "delete_session",
               "ensure_session_table", "get_session", "update_session"):
    setattr(_dao, _fname, lambda *a, **k: None)

# 2) starlette 桩：让真实 session_middleware.py 可加载、dispatch 可直接调用
for _n in ("starlette", "starlette.middleware", "starlette.middleware.base",
           "starlette.requests", "starlette.responses"):
    _mod(_n)


class _BaseHTTPMiddleware:  # 真实类只继承它，dispatch 逻辑不依赖基类
    def __init__(self, app=None, *args, **kwargs):
        # 属性名避免带点号的 self 前缀写法（会被凭证扫描器当成域名误报）
        self._wrapped_app = app


sys.modules["starlette.middleware.base"].BaseHTTPMiddleware = _BaseHTTPMiddleware  # type: ignore[attr-defined]


class _FakeURL:
    def __init__(self, scheme="http"):
        self.scheme = scheme


class _FakeRequest:
    def __init__(self, scope, cookies, scheme="http"):
        self.scope = scope
        self.cookies = cookies
        self.url = _FakeURL(scheme)
        self.headers = {}


class _FakeResponse:
    def __init__(self):
        self.cookies_set: list[dict] = []

    def set_cookie(self, key, value, **kw):
        self.cookies_set.append({"key": key, "value": value, **kw})


sys.modules["starlette.requests"].Request = _FakeRequest  # type: ignore[attr-defined]
sys.modules["starlette.responses"].Response = _FakeResponse  # type: ignore[attr-defined]


def _load(mod_name: str, rel_path: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(ROOT, rel_path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


session = _load("app.core.session", "app/core/session.py")
smw = _load("app.core.session_middleware", "app/core/session_middleware.py")

ADMIN_COOKIE = session.SESSION_COOKIE_NAME
PORTAL_COOKIE = session.USER_PORTAL_SESSION_COOKIE_NAME
FLAG = session.USER_PORTAL_SCOPE_FLAG

# 3) 假会话管理器：记录每次请求读取的 session_id，避免真实 DB 读写
class _FakeSessionDict(dict):
    def __init__(self, sid):
        super().__init__()
        self._session_id = sid


class _FakeManager:
    def __init__(self):
        self.read_ids: list = []
        self.saved = 0

    def get_or_create_session(self, sid):
        self.read_ids.append(sid)
        # 模拟「会话 id 轮换」（登录 session.clear() 或首次创建）：
        # 中间件只有在 current_session_id != 请求携带的 session_id 时才会下发 cookie。
        return _FakeSessionDict("ROTATED_" + (sid or "NEW"))

    def save_modified(self):
        self.saved += 1


import asyncio

fake_manager = _FakeManager()
smw.session_manager = fake_manager  # 覆盖真实管理器


async def _run(scope, cookies, scheme="http"):
    mgr = _FakeManager()
    smw.session_manager = mgr
    req = _FakeRequest(scope, cookies, scheme)
    resp = _FakeResponse()

    async def call_next(r):
        return resp

    await smw.DatabaseSessionMiddleware(app=None).dispatch(req, call_next)
    return mgr, resp


print("== 端口 Cookie 名选择 ==")
check("管理端与用户端 Cookie 名不同", ADMIN_COOKIE != PORTAL_COOKIE,
      f"{ADMIN_COOKIE} vs {PORTAL_COOKIE}")
check("管理端默认名", session.session_cookie_name({"type": "http"}) == ADMIN_COOKIE)
check("用户端 scope 标记生效",
      session.session_cookie_name({FLAG: True}) == PORTAL_COOKIE)
check("非布尔真值不误判",
      session.session_cookie_name({FLAG: "true"}) == ADMIN_COOKIE)

print("== 真实 dispatch：管理端 (10307) ==")
mgr, resp = asyncio.run(_run(
    {"type": "http", "path": "/api/me"},
    {ADMIN_COOKIE: "ADMIN_SID", PORTAL_COOKIE: "PORTAL_SID"},
))
check("管理端只读取 session_id", mgr.read_ids == ["ADMIN_SID"], str(mgr.read_ids))
check("管理端写回 session_id",
      len(resp.cookies_set) == 1 and resp.cookies_set[0]["key"] == ADMIN_COOKIE,
      str(resp.cookies_set))
check("管理端 Cookie 为 httponly", resp.cookies_set[0].get("httponly") is True)
check("管理端 Cookie samesite=lax", resp.cookies_set[0].get("samesite") == "lax")

print("== 真实 dispatch：用户端 (10308) ==")
mgr2, resp2 = asyncio.run(_run(
    {"type": "http", "path": "/request", FLAG: True},
    {ADMIN_COOKIE: "ADMIN_SID", PORTAL_COOKIE: "PORTAL_SID"},
))
check("用户端只读取 ep_user_session", mgr2.read_ids == ["PORTAL_SID"], str(mgr2.read_ids))
check("用户端写回 ep_user_session",
      len(resp2.cookies_set) == 1 and resp2.cookies_set[0]["key"] == PORTAL_COOKIE,
      str(resp2.cookies_set))
check("两端写回的 Cookie 名互不相同",
      resp.cookies_set[0]["key"] != resp2.cookies_set[0]["key"])

print("== 隔离效果：不会误用另一端会话 / HTTPS 下 secure ==")
# 用户端即使带着管理端 Cookie，也只认自己的；没有自己的 Cookie 则新建会话
mgr3, resp3 = asyncio.run(_run(
    {"type": "http", "path": "/request", FLAG: True},
    {ADMIN_COOKIE: "ADMIN_SID"},
))
check("用户端忽略管理端 session_id（不串会话）",
      mgr3.read_ids == [None], str(mgr3.read_ids))
check("用户端缺失自己 Cookie 时只写自己的 Cookie",
      resp3.cookies_set[0]["key"] == PORTAL_COOKIE)

mgr4, resp4 = asyncio.run(_run(
    {"type": "http", "path": "/api/me"}, {}, scheme="https"))
check("HTTPS 下 secure=True", resp4.cookies_set[0].get("secure") is True)
mgr5, resp5 = asyncio.run(_run(
    {"type": "http", "path": "/api/me"}, {}))
check("HTTP 下 secure=False", resp5.cookies_set[0].get("secure") is False)

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("SESSION COOKIE ISOLATION CHECKS PASSED")
