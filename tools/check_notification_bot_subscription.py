#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1.9.2 行为校验：入库事件订阅不再依赖 TG / 企业微信配置。

背景
----
只启用系统级 Bark、未配置 TG Token 与企微 corpid 时，旧版
NotificationBot.start() 会在 _subscribe_events() 之前直接 return；
EventBus.publish() 对无订阅者是静默空转（不报错、不记日志），于是
「新片入库」Bark 通知不触发，而工单自动流转（走 SystemDaemon）与
求片 Bark（submit_router 直连）都正常。

做法
----
不导入整个应用（依赖过重），而是用 AST 从真实源码里抽出
NotificationBot 的 start / _subscribe_events / _unsubscribe_events，
以及两个入库 handler 服务，放进受控命名空间执行，配合桩对象做
真实行为断言，最后用与事件总线同语义的同步 bus 把
「订阅 -> publish -> handler -> Bark」闭环跑一遍。

用法: python tools/check_notification_bot_subscription.py [ROOT]
"""
from __future__ import annotations

import ast
import collections
import datetime
import logging
import os
import re
import sys
import threading
import types

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
PASSED = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  " + name)
    else:
        FAILS.append(name + ("  " + detail if detail else ""))
        print("  FAIL  " + name + ("  " + detail if detail else ""))


def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class LoggerStub:
    def __init__(self):
        self.lines = []

    def info(self, *a, **kw):
        self.lines.append(a)

    def warning(self, *a, **kw):
        self.lines.append(a)

    def error(self, *a, **kw):
        self.lines.append(a)

    def debug(self, *a, **kw):
        pass


class RecordingBus:
    """与 app/core/event_bus.py 的 subscribe/publish 语义一致，但同步执行。"""

    def __init__(self):
        self.subscribed = []
        self.published = []
        self._handlers = {}

    def subscribe(self, event_type, handler):
        self.subscribed.append((event_type, handler))
        self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type, handler):
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def publish(self, event_type, *args, **kwargs):
        self.published.append(event_type)
        for handler in list(self._handlers.get(event_type, [])):
            handler(*args, **kwargs)


def load_notification_bot_methods() -> dict:
    tree = ast.parse(_src("app/bot/notification_bot/bot_service.py"))
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "NotificationBot"
    )
    wanted = {"start", "_subscribe_events", "_unsubscribe_events"}
    keep = [
        n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted
    ]
    module = ast.Module(body=keep, type_ignores=[])
    ns: dict = {}
    exec(compile(module, "<notification-bot-methods>", "exec"), ns)
    return ns


def load_library_common_stub():
    """2.0 B1：两个 handler service 的注入函数现在委托 `_library_common`。

    本脚本用 AST 抽取源码、会忽略 import 语句，因此这里加载真实公共模块的
    实现注入受控命名空间，保证断言跑的是产品代码而不是复刻桩。
    """
    import runpy

    path = os.path.join(ROOT, "app/bot/notification_bot/_library_common.py")
    return types.SimpleNamespace(**runpy.run_path(path))


def load_handler_service(rel: str) -> dict:
    """保留模块顶层函数与赋值，忽略 import；缺失的全局名由 ns 预置。"""
    tree = ast.parse(_src(rel))
    keep = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign))
    ]
    ns: dict = {
        "datetime": datetime,
        "re": re,
        "logging": logging,
        "defaultdict": collections.defaultdict,
        "_library_common": load_library_common_stub(),
        "REPORT_COVER_URL": "",
        "get_enable_library_notify": lambda: False,
        "get_media_server_host": lambda: "",
        "get_media_server_main_public_or_host": lambda: "",
    }
    module = ast.Module(body=keep, type_ignores=[])
    exec(compile(module, "<handler-service>", "exec"), ns)
    return ns


class FakeBot:
    def __init__(self, ns, bus):
        self.running = False
        self.poll_thread = None
        self._subscribed = False
        self._stop_event = threading.Event()
        self.ns = ns
        self.bus = bus
        self.calls = []
        self.handlers = {}

    def _subscribe_events(self):
        self.calls.append("_subscribe_events")
        self.ns["_subscribe_events"](self)

    def _unsubscribe_events(self):
        self.calls.append("_unsubscribe_events")
        self.ns["_unsubscribe_events"](self)

    def _set_commands(self):
        self.calls.append("_set_commands")

    def _set_wecom_menu(self):
        self.calls.append("_set_wecom_menu")

    def _polling_loop(self):
        pass

    def __getattr__(self, name):
        if name.startswith("on_") or name.startswith("_on_"):
            # 必须缓存：真实绑定方法每次访问都等价，unsubscribe 才能移除成功
            if name not in self.handlers:
                def handler(*a, _n=name, **kw):
                    self.calls.append("handled:" + _n)
                self.handlers[name] = handler
            return self.handlers[name]
        raise AttributeError(name)


def run_start(tg_token: str = "", wecom_corpid: str = ""):
    bus = RecordingBus()
    thread_log = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name
            self.started = False
            thread_log.append(self)

        def start(self):
            self.started = True

    logger_stub = LoggerStub()
    ns = load_notification_bot_methods()
    ns.update({
        "get_bot_tg_token": lambda: tg_token,
        "get_wecom_corpid": lambda: wecom_corpid,
        "threading": types.SimpleNamespace(Thread=FakeThread),
        "logger": logger_stub,
        "bus": bus,
    })
    bot = FakeBot(ns, bus)
    ns["start"](bot)
    return bot, bus, thread_log, logger_stub, ns


print("== 1.9.2: 只启用 Bark 时入库事件订阅仍建立 ==")
bot_src = _src("app/bot/notification_bot/bot_service.py")
check("start 不再因缺少 TG/企微提前 return",
      "if not get_bot_tg_token() and not get_wecom_corpid(): return" not in bot_src)

bot, bus, threads, logger_stub, ns = run_start("", "")
events = [e for e, _ in bus.subscribed]
check("无 TG/企微时 start 仍执行 _subscribe_events", "_subscribe_events" in bot.calls)
check("无 TG/企微时 notifier 仍进入 running", bot.running is True)
check("订阅 notify.library.new_item", "notify.library.new_item" in events)
check("订阅 notify.library.new_episode", "notify.library.new_episode" in events)
check("订阅登录 / 播放 / 删项等其它内部事件",
      all(e in events for e in ("notify.user.login", "notify.playback.start",
                                "notify.item.deleted", "notify.daily_report")))
check("无 TG/企微时不注册 TG 命令", "_set_commands" not in bot.calls)
check("无 TG/企微时不发企微菜单", "_set_wecom_menu" not in bot.calls)
check("无 TG/企微时不启动轮询线程", len(threads) == 0 and bot.poll_thread is None)
check("启动日志标注事件订阅已激活",
      any("event subscriptions active" in str(x) for x in logger_stub.lines))

print("== 凭证门控：有 TG / 企微时才做外部动作 ==")
bot_tg, _bus_tg, threads_tg, _, _ = run_start("stub_tg", "")
check("仅 TG：注册命令", "_set_commands" in bot_tg.calls)
check("仅 TG：不发企微菜单", "_set_wecom_menu" not in bot_tg.calls)
check("仅 TG：启动轮询线程", len(threads_tg) == 1 and threads_tg[0].started)
check("仅 TG：线程名为 notification-bot-polling",
      threads_tg[0].name == "notification-bot-polling")

bot_wc, _bus_wc, threads_wc, _, _ = run_start("", "stub_wecom")
check("仅企微：发企微菜单", "_set_wecom_menu" in bot_wc.calls)
check("仅企微：不注册 TG 命令", "_set_commands" not in bot_wc.calls)
check("仅企微：不启动轮询线程", len(threads_wc) == 0)

bot_all, _bus_all, threads_all, _, _ = run_start("stub_tg", "stub_wecom")
check("全部配置：命令与菜单都执行",
      "_set_commands" in bot_all.calls and "_set_wecom_menu" in bot_all.calls)
check("全部配置：启动轮询线程", len(threads_all) == 1)

print("== 幂等与取消订阅 ==")
bot_idem, bus_idem, _threads_idem, _, ns_idem = run_start("", "")
before = len(bus_idem.subscribed)
before_calls = bot_idem.calls.count("_subscribe_events")
ns_idem["start"](bot_idem)
check("重复 start 不重复订阅",
      len(bus_idem.subscribed) == before
      and bot_idem.calls.count("_subscribe_events") == before_calls)
bot_idem._unsubscribe_events()
check("stop 时取消 notify.library.new_item 订阅",
      not bus_idem._handlers.get("notify.library.new_item"))
check("stop 时取消 notify.library.new_episode 订阅",
      not bus_idem._handlers.get("notify.library.new_episode"))

print("== 事件链闭环：publish 入库事件 -> handler -> Bark ==")
bot_link, bus_link, _, _, _ = run_start("", "")
bark_calls = []


def fake_safe_push(kind, **kwargs):
    bark_calls.append((kind, kwargs))
    return {"ok": True, "skipped": False, "detail": "stub"}


def configure(service_ns):
    # 故意把「入库通知总开关」置为 False：Bark 是系统级独立通道，
    # 不应被旧总开关挡住，也不应触发后面的 TG / 企微发送分支。
    service_ns["set_dependency_providers"](
        enable_library_notify_provider=lambda: (lambda: False),
        media_quality_info_provider=lambda: (lambda _i: {}),
        media_server_main_public_or_host_provider=lambda: (lambda: ""),
        media_server_host_provider=lambda: (lambda: ""),
        notify_channels_provider=lambda: (lambda _t: []),
        get_plugin_provider=lambda: (lambda _n: None),
        report_cover_url_provider=lambda: "",
        datetime_provider=lambda: datetime,
        re_provider=lambda: re,
        logger_provider=lambda: LoggerStub(),
        bark_push_provider=lambda: fake_safe_push,
    )


svc_item = load_handler_service(
    "app/bot/notification_bot/notification_bot_library_new_item_service.py")
svc_episode = load_handler_service(
    "app/bot/notification_bot/notification_bot_library_new_episode_service.py")
configure(svc_item)
configure(svc_episode)

bus_link.subscribe(
    "notify.library.new_item",
    lambda item: svc_item["handle_library_new_item"](bot_link, item),
)
bus_link.subscribe(
    "notify.library.new_episode",
    lambda data: svc_episode["handle_library_new_episode"](bot_link, data),
)

bus_link.publish("notify.library.new_item", {
    "Id": "i1", "Name": "测试影片", "Type": "Movie",
    "ProductionYear": 2026, "CommunityRating": 8.1,
    "Overview": "<p>简介</p>", "ServerId": "s1",
})
bus_link.publish("notify.library.new_episode", {
    "series_id": "s1",
    "episodes": [{"ParentIndexNumber": 1, "IndexNumber": 2, "Name": "E02"}],
    "series_info": {
        "Name": "测试剧集", "ProductionYear": 2026,
        "CommunityRating": 7.5, "Overview": "简介", "ServerId": "s1",
    },
})

check("入库事件确实有订阅者", "handled:on_library_new_item" in bot_link.calls)
check("剧集事件确实有订阅者", "handled:on_library_new_episode" in bot_link.calls)
kinds = [k for k, _ in bark_calls]
check("电影入库触发 Bark library_new", "library_new" in kinds)
check("剧集入库同样触发 Bark library_new", kinds.count("library_new") >= 2)
check("Bark 不受入库通知总开关关闭影响", len(bark_calls) >= 2)
check("Bark 收到电影片名",
      any("测试影片" in str(v.get("name")) for _, v in bark_calls))
check("Bark 收到剧集名称与集数",
      any("测试剧集" in str(v.get("name")) for _, v in bark_calls))

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL NOTIFICATION BOT SUBSCRIPTION CHECKS PASSED")
