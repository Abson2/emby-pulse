# -*- coding: utf-8 -*-
"""用户级通知设置服务（10308 用户端口「我的 → 通知设置」）。

与 10307 管理端系统级 Bark 推送**互不干预**：本模块完全由用户自己配置
推送渠道（A. 用户自定义 Bark 链接 / B. 用户自定义 Webhook）与接收的内容类型，
面向用户本人，不影响管理端系统级推送。

设计约束（来自需求）：
- 推送内容**不附带任何网址**：经反代后无法推送最新网址，反而可能暴露真实端口；
- 任意单用户推送失败都被吞掉，绝不影响业务主流程（入库 / 消息 / 公告 / 心愿）。
"""
import json
import logging
import urllib.request

from app.domains.users import user_dao


logger = logging.getLogger("uvicorn")

_WEBHOOK_TIMEOUT = 10


def get_user_notify_settings(user_id: str):
    return user_dao.get_user_notify_settings(user_id)


def save_user_notify_settings(user_id: str, settings: dict):
    user_dao.save_user_notify_settings(user_id, settings)


def _parse_bark_url(bark_url: str):
    """将用户填写的 Bark 推送链接（https://<server>/<device-key>）拆为 server + device_key。"""
    u = (bark_url or "").strip().rstrip("/")
    if not u or not u.startswith(("http://", "https://")):
        return None, None
    if "/" in u[8:]:
        server, _, key = u.rpartition("/")
    else:
        return None, None
    if not key:
        return None, None
    return server, key


def push_user_notify(user_id: str, title: str, body: str, flag_key: str = None) -> dict:
    """按用户配置向其自选渠道（Bark / Webhook）推送一条消息。

    flag_key 非空时，仅当用户开启了该内容开关才推送（否则直接 skipped）。
    任何失败都被吞掉，返回结构化结果，绝不向业务主流程抛异常。
    """
    try:
        cfg = user_dao.get_user_notify_settings(user_id)
        if flag_key and not cfg.get(flag_key):
            return {"ok": False, "skipped": True, "detail": "用户未开启该内容通知"}

        channel = cfg.get("channel")
        if channel == "bark":
            server, key = _parse_bark_url(cfg.get("bark_url", ""))
            if not server or not key:
                return {"ok": False, "skipped": True, "detail": "Bark 链接无效"}
            try:
                from app.domains.notifications.bark_push import send as bark_send
                return bark_send(
                    title, body,
                    config={"server": server, "device_key": key, "enabled": True},
                )
            except Exception as e:
                logger.warning(f"[用户通知] Bark 推送失败(user={user_id}): {e}")
                return {"ok": False, "skipped": True, "detail": str(e)}
        elif channel == "webhook":
            url = (cfg.get("webhook_url") or "").strip()
            if not url:
                return {"ok": False, "skipped": True, "detail": "Webhook 未配置"}
            try:
                payload = json.dumps(
                    {"title": title, "body": body, "text": f"{title}\n{body}"}
                ).encode("utf-8")
                req = urllib.request.Request(
                    url, data=payload, method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT) as resp:
                    status = resp.status
                return {"ok": status in (200, 201, 204), "skipped": False, "detail": f"HTTP {status}"}
            except Exception as e:
                logger.warning(f"[用户通知] Webhook 推送失败(user={user_id}): {e}")
                return {"ok": False, "skipped": True, "detail": str(e)}
        else:
            return {"ok": False, "skipped": True, "detail": "用户未配置推送渠道"}
    except Exception as e:
        logger.warning(f"[用户通知] 推送异常(user={user_id}): {e}")
        return {"ok": False, "skipped": True, "detail": str(e)}


def notify_users_by_flag(flag_key: str, title: str, body: str):
    """向所有开启某内容开关的用户广播推送（新片入库 / 全站公告等广播类）。"""
    try:
        user_ids = user_dao.list_users_with_notify_flag(flag_key)
        for uid in user_ids:
            try:
                push_user_notify(uid, title, body, flag_key=flag_key)
            except Exception as e:
                logger.warning(f"[用户通知] 广播推送失败(user={uid}): {e}")
    except Exception as e:
        logger.warning(f"[用户通知] 广播查询失败: {e}")


def notify_request_users(tmdb_id, season, flag_key: str, title: str, body: str):
    """仅向请求过该影视的用户推送心愿清单状态变动（有变动才推，无变动不推）。"""
    try:
        user_ids = user_dao.list_request_users(tmdb_id, season)
        for uid in user_ids:
            try:
                push_user_notify(uid, title, body, flag_key=flag_key)
            except Exception as e:
                logger.warning(f"[用户通知] 心愿推送失败(user={uid}): {e}")
    except Exception as e:
        logger.warning(f"[用户通知] 心愿查询失败: {e}")
