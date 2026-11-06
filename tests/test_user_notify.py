#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用户端口「通知设置」回归测试（需求 5，1.9.3）。

不用 pytest 收集，直接运行：
    python tests/test_user_notify.py

覆盖：
- flag 门控：未开启对应内容开关 -> skipped，不调用任何渠道；
- Bark 分支：开启 + 配置合法 -> 调用 bark_push.send（且 url=None、正文不含 http）；
- Webhook 分支：开启 + 配置合法 -> 调用 urlopen（桩，HTTP 200）；
- 广播 notify_users_by_flag：只对开启开关的用户推送；
- 心愿 notify_request_users：只对「请求过该 tmdb/season 且开启开关」的用户推送；
- 不含网址：服务层本身不为正文追加任何 URL（设计约束：反代后无法推送最新网址）。

纯逻辑测试，不依赖数据库 / 真实 Bark / 真实 Webhook。
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types
import urllib.request

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, _REPO_ROOT)

PASSED = 0
FAILED = []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  " + name)
    else:
        FAILED.append(name + ("  " + detail if detail else ""))
        print("  FAIL  " + name + ("  " + detail if detail else ""))


# ---- 构造轻量桩：避免拉起 fastapi 等重依赖 ----
# user_dao 经由 schema_bootstrap -> playback_store -> app.core.config 间接引入 fastapi，
# 因此这里用桩模块替换 user_dao，再从文件加载被测模块，保证纯标准库即可运行。
_DEFAULTS = {
    "channel": "none", "bark_url": "", "webhook_url": "",
    "notify_new_item": False, "notify_admin_message": False,
    "notify_announcement": False, "notify_wishlist": False,
}

_fake_app = types.ModuleType("app")
_fake_dom = types.ModuleType("app.domains")
_fake_users = types.ModuleType("app.domains.users")
_fake_dao = types.ModuleType("app.domains.users.user_dao")
sys.modules.setdefault("app", _fake_app)
sys.modules.setdefault("app.domains", _fake_dom)
sys.modules["app.domains.users"] = _fake_users
sys.modules["app.domains.users.user_dao"] = _fake_dao

_DAO = None  # 由 setup_module 注入 FakeDao 实例


def _get_user_notify_settings(user_id):
    return _DAO.get_user_notify_settings(user_id) if _DAO else dict(_DEFAULTS)


def _list_users_with_notify_flag(flag_key):
    return _DAO.list_users_with_notify_flag(flag_key) if _DAO else []


def _list_request_users(tmdb_id, season):
    return _DAO.list_request_users(tmdb_id, season) if _DAO else []


_fake_dao.get_user_notify_settings = _get_user_notify_settings
_fake_dao.list_users_with_notify_flag = _list_users_with_notify_flag
_fake_dao.list_request_users = _list_request_users

# bark_push 同样经重依赖链引入，这里用桩模块替换其 send，避免拉起真实包链。
# push_user_notify 内部 `from app.domains.notifications.bark_push import send` 会命中 sys.modules 中的桩。
_fake_bark = types.ModuleType("app.domains.notifications.bark_push")


def _fake_bark_send(title, body, url=None, config=None):
    return rec.bark_send(title, body, url, config)


_fake_bark.send = _fake_bark_send
_orig_bark_mod = sys.modules.get("app.domains.notifications.bark_push")
sys.modules["app.domains.notifications.bark_push"] = _fake_bark

_spec = importlib.util.spec_from_file_location(
    "app.domains.users.user_notify_service",
    os.path.join(_REPO_ROOT, "app", "domains", "users", "user_notify_service.py"),
)
uns = importlib.util.module_from_spec(_spec)
sys.modules["app.domains.users.user_notify_service"] = uns
_spec.loader.exec_module(uns)

_DEFAULTS = {
    "channel": "none", "bark_url": "", "webhook_url": "",
    "notify_new_item": False, "notify_admin_message": False,
    "notify_announcement": False, "notify_wishlist": False,
}


def make_settings(**kw):
    s = dict(_DEFAULTS)
    s.update(kw)
    return s


class FakeDao:
    def __init__(self, users=None, requests=None):
        self._users = users or []        # [{"user_id", "settings"}]
        self._requests = requests or []  # [{"user_id","tmdb_id","season"}]

    def get_user_notify_settings(self, user_id):
        for u in self._users:
            if u["user_id"] == user_id:
                return u["settings"]
        return dict(_DEFAULTS)

    def list_users_with_notify_flag(self, flag_key):
        return [u["user_id"] for u in self._users if u["settings"].get(flag_key)]

    def list_request_users(self, tmdb_id, season):
        return [
            r["user_id"] for r in self._requests
            if r.get("tmdb_id") == tmdb_id and r.get("season") == season
        ]


class CallRecorder:
    def __init__(self):
        self.bark = []
        self.webhook = []

    def bark_send(self, title, body, url=None, config=None):
        self.bark.append((title, body, url, config))
        return {"ok": True, "skipped": False, "detail": "sent"}

    def urlopen(self, req, timeout=None):
        class _R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return _R()


rec = CallRecorder()
_orig_urlopen = None


def setup_module(dao):
    global _orig_urlopen, _DAO
    _DAO = dao
    _orig_urlopen = urllib.request.urlopen
    urllib.request.urlopen = rec.urlopen


def teardown_module():
    if _orig_bark_mod is None:
        sys.modules.pop("app.domains.notifications.bark_push", None)
    else:
        sys.modules["app.domains.notifications.bark_push"] = _orig_bark_mod
    if _orig_urlopen is not None:
        urllib.request.urlopen = _orig_urlopen


def test_flag_gating_disabled():
    rec.bark.clear()
    dao = FakeDao(users=[{"user_id": "u1", "settings": make_settings(
        channel="bark", bark_url="https://bark.example/x", notify_admin_message=False)}])
    setup_module(dao)
    res = uns.push_user_notify("u1", "管理员消息", "内容", flag_key="notify_admin_message")
    check("未开启开关时 skipped", res.get("skipped") is True, str(res))
    check("未开启时不调用 Bark", len(rec.bark) == 0, str(rec.bark))


def test_bark_branch_enabled():
    rec.bark.clear()
    dao = FakeDao(users=[{"user_id": "u1", "settings": make_settings(
        channel="bark", bark_url="https://bark.example/x", notify_admin_message=True)}])
    setup_module(dao)
    res = uns.push_user_notify("u1", "管理员消息", "内容", flag_key="notify_admin_message")
    check("开启后推送成功", res.get("ok") is True, str(res))
    check("调用了 Bark send", len(rec.bark) == 1, str(rec.bark))
    title, body, url, config = rec.bark[0]
    check("Bark 不携带网址(url=None)", url is None, str(url))
    check("Bark 正文不含 http", "http" not in body, body)


def test_webhook_branch_enabled():
    rec.webhook.clear()
    dao = FakeDao(users=[{"user_id": "u2", "settings": make_settings(
        channel="webhook", webhook_url="https://hook.example/post", notify_announcement=True)}])
    setup_module(dao)
    res = uns.push_user_notify("u2", "全站公告", "内容", flag_key="notify_announcement")
    check("Webhook 开启后推送成功", res.get("ok") is True, str(res))


def test_notify_users_by_flag_only_enabled():
    rec.bark.clear()
    dao = FakeDao(users=[
        {"user_id": "on", "settings": make_settings(channel="bark", bark_url="https://bark.example/x", notify_new_item=True)},
        {"user_id": "off", "settings": make_settings(channel="bark", bark_url="https://bark.example/x", notify_new_item=False)},
    ])
    setup_module(dao)
    uns.notify_users_by_flag("notify_new_item", "新片入库", "正文")
    check("广播只推送给开启用户(共 1 次)", len(rec.bark) == 1, str(rec.bark))
    if rec.bark:
        check("广播标题正确", rec.bark[0][0] == "新片入库", str(rec.bark[0][0]))


def test_notify_request_users_only_requesters():
    rec.bark.clear()
    dao = FakeDao(
        users=[
            {"user_id": "req_on", "settings": make_settings(channel="bark", bark_url="https://bark.example/x", notify_wishlist=True)},
            {"user_id": "req_off", "settings": make_settings(channel="bark", bark_url="https://bark.example/x", notify_wishlist=False)},
            {"user_id": "other", "settings": make_settings(channel="bark", bark_url="https://bark.example/x", notify_wishlist=True)},
        ],
        requests=[
            {"user_id": "req_on", "tmdb_id": 123, "season": 1},
            {"user_id": "req_off", "tmdb_id": 123, "season": 1},
        ],
    )
    setup_module(dao)
    uns.notify_request_users(123, 1, "notify_wishlist", "求片状态更新", "已入库")
    check("心愿只推送给请求者且开启开关(共 1 次)", len(rec.bark) == 1, str(rec.bark))
    if rec.bark:
        check("心愿推送标题正确", rec.bark[0][0] == "求片状态更新", str(rec.bark[0][0]))


def test_no_url_appended_by_service():
    body_in = "影视《测试》已入库，请前往观看"
    dao = FakeDao(users=[{"user_id": "u1", "settings": make_settings(
        channel="bark", bark_url="https://bark.example/x", notify_new_item=True)}])
    setup_module(dao)
    rec.bark.clear()
    uns.push_user_notify("u1", "新片入库", body_in, flag_key="notify_new_item")
    check("服务层不为正文追加 URL",
          rec.bark and rec.bark[0][2] is None and "http" not in rec.bark[0][1],
          str(rec.bark))


if __name__ == "__main__":
    try:
        test_flag_gating_disabled()
        test_bark_branch_enabled()
        test_webhook_branch_enabled()
        test_notify_users_by_flag_only_enabled()
        test_notify_request_users_only_requesters()
        test_no_url_appended_by_service()
    finally:
        teardown_module()
    print("\n通过 %d / 失败 %d" % (PASSED, len(FAILED)))
    if FAILED:
        for f in FAILED:
            print("  - " + f)
        sys.exit(1)
    print("USER NOTIFY CHECKS PASSED")
