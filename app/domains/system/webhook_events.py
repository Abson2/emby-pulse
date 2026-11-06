# -*- coding: utf-8 -*-
"""Emby Webhook 事件名归一化与 LibraryChanged 拆条（1.9.8）。

背景
----
Emby Webhooks 插件发出的事件名是 ``ItemAdded`` / ``LibraryChanged`` /
``PlaybackStart`` 这类驼峰形式，而本项目下游只认 ``item.added`` /
``library.new`` / ``playback.start`` 等内部事件名；并且 ``LibraryChanged``
用 ``ItemsAdded`` **数组**承载新增条目，而不是单个 ``Item``。两者对不上时，
入库事件在 ``notification_bot_webhook_event_service`` 里被静默丢弃，
系统级 Bark 与用户端「新片入库」广播都不会触发。

本模块只做纯数据转换，不依赖 FastAPI / 事件总线，便于用标准库做行为回归。
转换后的发布动作仍在 ``webhook.py::emby_webhook`` 里完成。

明细补全：拆条载荷可以只带 ``Item.Id``，下游 ``push_single_item`` 会调
Emby API ``/Items/{Id}`` 补全片名 / 类型 / 简介，因此不会推出「未知影片」。
"""
from __future__ import annotations

import threading
import time

# Emby 原生事件名（去分隔符 + 小写）-> 项目内部事件名
EMBY_TO_INTERNAL = {
    "itemadded": "item.added",
    "libraryitemadded": "item.added",
    "librarychanged": "library.new",
    "playbackstart": "playback.start",
    "playbackstop": "playback.stop",
    "authenticationsuccess": "login",
    "userauthenticated": "login",
    "itemremoved": "remove",
    "itemdeleted": "remove",
    "libraryitemremoved": "remove",
    "libraryitemdeleted": "remove",
}

# 已是内部格式的事件名：保持原样，避免归一化把点号打散后反而匹配不上
KNOWN_INTERNAL_EVENTS = {
    "item.added", "library.new", "library.updated",
    "playback.start", "playback.stop",
    "auth", "login", "delete", "remove",
}

# 已知可忽略的 Emby 原生事件：既不是入库事件，也不承载业务动作
# （不触发通知 / 播放统计 / 违规拦截）。列入集合后：
#   1) 不再打印「未识别 Event=」限流告警，避免日志被测试 / 系统事件刷屏；
#   2) webhook.py 会直接 return，不向事件总线发布，避免 library.deleted 等
#      含 "delete" 子串的事件被下游 "delete" in event 误判成「媒体被删除」通知。
# 只放「确实非入库、无业务动作」的事件；若日后出现新的入库类事件，
# 应加入 EMBY_TO_INTERNAL，而不是放进这里。
IGNORE_EVENT_KEYS = {
    "librarydeleted",          # LibraryDeleted：整个媒体库被移除，≠ 单项媒体删除
    "systemnotificationtest",  # Emby 后台「发送测试通知」按钮
    "systemwebhooktest",       # Webhook 自测
    "sessionstarted",          # 会话开始（高频，无业务动作）
    "sessionstopped",          # 会话结束
    "userdatasaved",           # 用户评分 / 进度保存（高频，无业务动作）
}

_RECENT_TTL = 300.0        # 5 分钟内同一 Item.Id 只入队一次
_RECENT_MAX = 4096
_recent_lock = threading.Lock()
_recent_items = {}          # item_id -> monotonic 时间戳
_unseen_lock = threading.Lock()
_unseen_events = set()      # 未识别事件名，按名称限流告警


def event_key(raw) -> str:
    """把事件名归一化为无分隔符小写，供映射表查询。"""
    if not isinstance(raw, str):
        return ""
    return (raw.strip().lower()
            .replace(" ", "").replace("-", "")
            .replace("_", "").replace(".", ""))


def get_raw_event(data: dict) -> str:
    """兼容 Emby 插件可能使用的几种事件名字段。"""
    if not isinstance(data, dict):
        return ""
    for key in ("Event", "event", "EventType", "event_type"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def normalize_event(raw) -> str:
    """Emby 原生事件名 -> 内部事件名；已是内部格式或未收录时保持小写原样。"""
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if text in KNOWN_INTERNAL_EVENTS:
        return text
    return EMBY_TO_INTERNAL.get(event_key(text), text)


def is_library_changed(raw) -> bool:
    return event_key(raw) == "librarychanged"


def is_known_event(normalized: str) -> bool:
    return normalized in KNOWN_INTERNAL_EVENTS


def is_ignorable_event(raw) -> bool:
    """Emby 测试 / 系统 / 会话类事件：已知非业务，可安全忽略（不告警、不发布）。"""
    return event_key(raw) in IGNORE_EVENT_KEYS


def split_library_changed(data: dict):
    """把 LibraryChanged 的 ItemsAdded 数组拆成多条 item.added 载荷。

    ``ItemsAdded`` 元素可能是字符串 Id，也可能是带 Id / Type / SeriesId 的
    对象；后者原样保留字段，利于下游剧集分组与季海报插件识别剧集类型。

    Returns:
        [(item_id, payload_dict), ...]
    """
    if not isinstance(data, dict):
        return []
    raw_items = data.get("ItemsAdded")
    if raw_items is None:
        raw_items = data.get("items_added")
    if not isinstance(raw_items, list):
        raw_items = [raw_items] if raw_items else []

    out = []
    for raw in raw_items:
        if isinstance(raw, str):
            item_id = raw.strip()
            item = {"Id": item_id}
        elif isinstance(raw, dict):
            item_id = str(raw.get("Id") or "").strip()
            item = dict(raw)
        else:
            continue
        if not item_id:
            continue
        payload = dict(data)
        payload["Event"] = "item.added"
        payload["Item"] = item
        out.append((item_id, payload))
    return out


def mark_item_seen(item_id, *, now=None, ttl=_RECENT_TTL) -> bool:
    """进程内短期去重；返回 True 表示该 Item.Id 在窗口内已处理过。

    Emby 常同时勾选 ``Item Added`` 与 ``Library Changed``，同一部片会收到
    两次事件。``add_library_task`` 只能去重同一批队列，这里补一个跨批次
    的短窗口，避免用户收到两条 Bark。
    """
    if not item_id:
        return False
    moment = time.monotonic() if now is None else now
    with _recent_lock:
        if len(_recent_items) >= _RECENT_MAX:
            for key, ts in list(_recent_items.items()):
                if moment - ts > ttl:
                    _recent_items.pop(key, None)
        previous = _recent_items.get(item_id)
        if previous is not None and moment - previous < ttl:
            return True
        _recent_items[item_id] = moment
        return False


def warn_unknown_event_once(raw, normalized, logger=None) -> bool:
    """按事件名限流告警，避免 SessionStarted 等高频事件刷屏。"""
    key = str(raw or normalized or "")
    if not key:
        return False
    with _unseen_lock:
        if key in _unseen_events:
            return False
        _unseen_events.add(key)
    if logger is not None:
        try:
            logger.warning(
                "[Webhook] 未识别 Event=%s (归一化=%s)；若为入库事件请补充映射表",
                key, normalized,
            )
        except Exception:
            pass
    return True


def reset_dedupe_cache() -> None:
    """仅供回归测试重置进程内状态。"""
    with _recent_lock:
        _recent_items.clear()
    with _unseen_lock:
        _unseen_events.clear()
