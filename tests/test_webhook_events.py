#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Emby Webhook 事件名归一化 / LibraryChanged 拆条（1.9.8）纯标准库行为回归。

不用 pytest 收集，直接运行：
    python tests/test_webhook_events.py

覆盖：
- Emby 原生事件名（ItemAdded / LibraryChanged / PlaybackStart …）→ 内部事件名；
- LibraryChanged 的 ItemsAdded 数组（字符串 Id / 对象 / 脏数据）拆条；
- Item Added + Library Changed 双事件短期去重，避免双推 Bark；
- 未识别事件按名称限流告警；
- **端到端**：把 Emby 原生载荷送进下游 handle_webhook_event，验证入库任务真的入队。

用桩注入 app.core.event_bus，不拉起 FastAPI / 应用依赖。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

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


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 依赖桩
# ---------------------------------------------------------------------------
for pkg in ("app", "app.core", "app.domains", "app.domains.system",
            "app.bot", "app.bot.notification_bot"):
    if pkg not in sys.modules:
        mod = types.ModuleType(pkg)
        mod.__path__ = []
        sys.modules[pkg] = mod

_event_bus_stub = types.ModuleType("app.core.event_bus")
_event_bus_stub.bus = types.SimpleNamespace(
    publish=lambda *a, **k: None,
    subscribe=lambda *a, **k: None,
)
sys.modules["app.core.event_bus"] = _event_bus_stub

we = load_module(
    "ep_webhook_events_under_test",
    os.path.join(_REPO_ROOT, "app", "domains", "system", "webhook_events.py"),
)
svc = load_module(
    "ep_webhook_event_service_under_test",
    os.path.join(_REPO_ROOT, "app", "bot", "notification_bot",
                 "notification_bot_webhook_event_service.py"),
)


class _SilentLogger:
    def __init__(self):
        self.warnings = []

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        self.warnings.append(a)


class _Daemon:
    def __init__(self):
        self.items = []

    def add_library_task(self, item):
        self.items.append(item)

    def _clear_gap_record_async(self, item):
        pass


svc.set_dependency_providers(
    bus_provider=lambda: _event_bus_stub.bus,
    logger_provider=lambda: _SilentLogger(),
    calendar_service_provider=lambda: types.SimpleNamespace(
        mark_episode_ready=lambda *a: None
    ),
)

print("== 1. 事件名归一化（Emby 原生 -> 内部） ==")
_cases = [
    ("ItemAdded", "item.added"),
    ("item.added", "item.added"),
    ("ITEM ADDED", "item.added"),
    ("item_added", "item.added"),
    ("LibraryItemAdded", "item.added"),
    ("LibraryChanged", "library.new"),
    ("library.new", "library.new"),
    ("PlaybackStart", "playback.start"),
    ("playback.start", "playback.start"),
    ("PlaybackStop", "playback.stop"),
    ("AuthenticationSuccess", "login"),
    ("UserAuthenticated", "login"),
    ("ItemRemoved", "remove"),
    ("ItemDeleted", "remove"),
    ("SessionStarted", "sessionstarted"),
    ("", ""),
]
for _raw, _expect in _cases:
    check("归一化 %r -> %r" % (_raw, _expect),
          we.normalize_event(_raw) == _expect,
          "实际 %r" % we.normalize_event(_raw))
check("已是内部格式的事件不会被打散",
      we.normalize_event("library.updated") == "library.updated")
check("已知事件判定", we.is_known_event("item.added") and not we.is_known_event("sessionstarted"))

print("== 2. 原始事件名字段兼容 ==")
check("Event 字段", we.get_raw_event({"Event": "ItemAdded"}) == "ItemAdded")
check("event 小写字段", we.get_raw_event({"event": "LibraryChanged"}) == "LibraryChanged")
check("EventType 字段", we.get_raw_event({"EventType": "ItemAdded"}) == "ItemAdded")
check("无事件字段返回空", we.get_raw_event({}) == "")
check("LibraryChanged 判定", we.is_library_changed("LibraryChanged")
      and not we.is_library_changed("ItemAdded"))

print("== 3. LibraryChanged 的 ItemsAdded 数组拆条 ==")
_data = {
    "Event": "LibraryChanged",
    "Server": {"Name": "NAS"},
    "ItemsAdded": [
        "id-1",
        "id-2",
        {"Id": "id-3", "Type": "Movie", "Name": "英雄连"},
        {"Name": "缺 Id 应跳过"},
        123,
    ],
}
_out = we.split_library_changed(_data)
check("拆出 3 条有效载荷（脏数据被跳过）", len(_out) == 3, "实际 %d" % len(_out))
check("每条 Event 改为 item.added",
      all(p.get("Event") == "item.added" for _, p in _out))
check("字符串元素补成 Item.Id", _out[0][1]["Item"] == {"Id": "id-1"})
check("对象元素保留 Type/Name，利于剧集分组与季海报插件",
      _out[2][1]["Item"].get("Type") == "Movie"
      and _out[2][1]["Item"].get("Name") == "英雄连")
check("原始载荷其它字段保留", _out[0][1].get("Server", {}).get("Name") == "NAS")
check("无 ItemsAdded 时返回空", we.split_library_changed({"Event": "LibraryChanged"}) == [])
check("items_added 小写字段兼容",
      len(we.split_library_changed({"Event": "LibraryChanged", "items_added": ["x"]})) == 1)
check("单个字符串（非数组）也处理",
      len(we.split_library_changed({"Event": "LibraryChanged", "ItemsAdded": "solo"})) == 1)
check("非 dict 载荷安全返回", we.split_library_changed(None) == [])

print("== 4. 双事件短期去重（避免同一部片推两条 Bark） ==")
we.reset_dedupe_cache()
check("首次出现不重复", we.mark_item_seen("A", now=0.0) is False)
check("窗口内重复出现", we.mark_item_seen("A", now=10.0) is True)
check("不同 Id 互不干扰", we.mark_item_seen("B", now=10.0) is False)
check("超过 TTL 后可再发", we.mark_item_seen("A", now=400.0) is False)
we.reset_dedupe_cache()
check("重置后状态清空", we.mark_item_seen("A", now=0.0) is False)

print("== 5. 未识别事件按名称限流告警 ==")
_we_log = _SilentLogger()
we.reset_dedupe_cache()
check("首次告警", we.warn_unknown_event_once("SessionStarted", "sessionstarted", _we_log) is True)
check("同名不再告警", we.warn_unknown_event_once("SessionStarted", "sessionstarted", _we_log) is False)
check("不同事件名告警", we.warn_unknown_event_once("UserDataSaved", "userdatasaved", _we_log) is True)
check("高频事件只产生 2 条告警", len(_we_log.warnings) == 2, str(len(_we_log.warnings)))

print("== 6. 端到端：Emby 原生载荷 -> 入库任务入队 ==")
# 6.1 ItemAdded（1.9.7 会被静默丢弃）
_d1 = _Daemon()
_raw = we.get_raw_event({"Event": "ItemAdded", "Item": {"Id": "m1", "Name": "英雄连", "Type": "Movie"}})
_event = we.normalize_event(_raw)
svc.handle_webhook_event(_d1, _event, {"Event": "ItemAdded", "Item": {"Id": "m1", "Name": "英雄连", "Type": "Movie"}})
check("ItemAdded 归一化后入队", _event == "item.added" and len(_d1.items) == 1, str(_d1.items))
check("入队条目保留片名", (_d1.items[0].get("Name") if _d1.items else "") == "英雄连")

# 6.2 LibraryChanged 拆条逐条入队
_d2 = _Daemon()
_lib_payload = {
    "Event": "LibraryChanged",
    "ItemsAdded": ["m2", "m3", {"Id": "m4", "Type": "Movie"}],
}
for _iid, _p in we.split_library_changed(_lib_payload):
    svc.handle_webhook_event(_d2, "item.added", _p)
check("LibraryChanged 3 条全部入队", len(_d2.items) == 3, str(_d2.items))
check("入队 Id 顺序正确", [x.get("Id") for x in _d2.items] == ["m2", "m3", "m4"])

# 6.3 复现旧缺陷：未归一化的驼峰事件名不会入队
_d3 = _Daemon()
svc.handle_webhook_event(_d3, "itemadded",
                         {"Event": "ItemAdded", "Item": {"Id": "m1", "Name": "英雄连"}})
check("未归一化的 itemadded 不入队（复现 1.9.7 缺陷）", len(_d3.items) == 0, str(_d3.items))

# 6.4 只有 Id 也能入队（明细由 push_single_item 经 Emby API 补全）
_d4 = _Daemon()
svc.handle_webhook_event(_d4, "item.added", {"Event": "item.added", "Item": {"Id": "only-id"}})
check("只有 Item.Id 也能入队", len(_d4.items) == 1)

print("== 7. 已知非业务事件抑制（1.9.9） ==")
# 两类真实告警：library.deleted（整个媒体库被移除，非单项媒体删除）、
# system.notificationtest（Emby 后台「发送测试通知」按钮）。两者都不是入库事件，
# 应按用户提示「若为入库事件请补充映射表」——它们不是，应被静默忽略，不刷屏告警、
# 也不向事件总线发布（否则 library.deleted 会被下游 "delete" in event 误判成媒体删除通知）。
check("library.deleted 被判为可忽略", we.is_ignorable_event("library.deleted"))
check("LibraryDeleted 大小写不敏感", we.is_ignorable_event("LibraryDeleted"))
check("system.notificationtest 被判为可忽略", we.is_ignorable_event("system.notificationtest"))
check("SessionStarted 被判为可忽略（高频会话事件）", we.is_ignorable_event("SessionStarted"))
check("ItemAdded 不是可忽略（是入库）", not we.is_ignorable_event("ItemAdded"))
check("LibraryChanged 不是可忽略（是入库拆条入口）", not we.is_ignorable_event("LibraryChanged"))
check("ItemRemoved 不是可忽略（确实是媒体删除，应走 remove 映射）",
      not we.is_ignorable_event("ItemRemoved"))

# 复刻 webhook.py 的告警门控：未知 + 非已知 + 非可忽略 才告警
def _should_warn(raw, normalized):
    return bool(normalized) and not we.is_known_event(normalized) \
        and not we.is_ignorable_event(raw)

check("library.deleted 不触发告警也不发布",
      not _should_warn("library.deleted", we.normalize_event("library.deleted")))
check("system.notificationtest 不触发告警也不发布",
      not _should_warn("system.notificationtest", we.normalize_event("system.notificationtest")))
check("归一化后 library.deleted 仍未进内部事件集（确认只能靠忽略门控拦截）",
      we.normalize_event("library.deleted") == "library.deleted"
      and not we.is_known_event("library.deleted"))
check("真正未知的 foo.bar 仍会按名称限流告警",
      _should_warn("foo.bar", we.normalize_event("foo.bar")))

print("\n通过 %d / 失败 %d" % (PASSED, len(FAILED)))
if FAILED:
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("EMBY WEBHOOK EVENT CHECKS PASSED")
