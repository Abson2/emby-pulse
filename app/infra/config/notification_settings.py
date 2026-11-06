from app.core.config import cfg
import json


# ---------------------------------------------------------------------------
# 系统级 Bark 推送（1.9.0）：新片入库 / 新的求片
# ---------------------------------------------------------------------------
BARK_SERVER_DEFAULT = "https://api.day.app"
BARK_GROUP_DEFAULT = "EMBYPULSE"
BARK_SOUND_DEFAULT = "default"

# 🔥 1.9.7 配置边界说明（避免概念混淆）：
# .env 里的 WEBHOOK_TOKEN 是 Emby Webhook 回调地址 ?token= 的**接口校验凭证**，
# 属于「入库事件入口」链路；Bark 的服务器 / Device Key / 开关属于「推送出口」
# 链路，只从 config.json 读写。两条链路没有任何配置耦合，不能用 WEBHOOK_TOKEN
# 推断 Bark 是否已配置，也不能把 Bark 不推送归因于 .env。
# 本模块不读取任何 BARK_* 环境变量，行为与 1.9.6 一致。


def _mask_secret(value):
    if not value or not isinstance(value, str):
        return value
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


def _bark_bool(value, default):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def get_bark_config(*, masked: bool = True) -> dict:
    """系统级 Bark 配置。管理端读取时 Device Key 做掩码，运行期读取原文。

    配置源恒为 config.json（见文件头「配置边界说明」），不引入环境变量优先级，
    保证既有管理端读写行为与 1.9.6 完全一致。
    """
    try:
        key = str(cfg.get("bark_device_key", "") or "")
    except Exception:
        key = ""
    return {
        "enabled": _bark_bool(cfg.get("bark_enabled", False), False),
        "server": str(cfg.get("bark_server", BARK_SERVER_DEFAULT)
                      or BARK_SERVER_DEFAULT).strip().rstrip("/"),
        "device_key": _mask_secret(key) if masked else key,
        "sound": str(cfg.get("bark_sound", BARK_SOUND_DEFAULT) or BARK_SOUND_DEFAULT).strip(),
        "group": str(cfg.get("bark_group", BARK_GROUP_DEFAULT) or BARK_GROUP_DEFAULT).strip(),
        "notify_library_new": _bark_bool(cfg.get("bark_notify_library_new", True), True),
        "notify_request_new": _bark_bool(cfg.get("bark_notify_request_new", True), True),
    }


def get_bark_runtime_config() -> dict:
    return get_bark_config(masked=False)


def _parse_channels(raw):
    """把 channels 字段（JSON 字符串 / list）统一解析为 list[str]。"""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (ValueError, TypeError):
            pass
    return []


def is_bark_channel_selected(notify_type: str = "library_new") -> bool:
    """🔥 1.9.7：判断指定通知类型的渠道勾选里是否包含 Bark。

    借鉴企业微信「channels 列表驱动」模型：1.9.5 起前端在「推送场景总控」
    与「工单大厅 / 用户管理通知设置」里都能勾选 bark，但后端完全不消费，
    勾了等于没勾。本函数让该勾选成为 Bark 的合法启用来源。

    - ``library_new``：与企微 / TG 同源，优先读「推送场景总控」
      ``library_notify_channels``；总控未配置时才回退 notify_rules 表。
    - 其他类型：读 notify_rules 对应行，且要求该规则处于启用状态。

    只做只读判断、异常一律返回 False，绝不因渠道配置异常影响业务主流程。
    """
    if notify_type == "library_new":
        channels = _parse_channels(cfg.get("library_notify_channels", "") or "")
        if channels:
            return "bark" in channels
    try:
        from app.domains.notifications.notify_admin_dao import get_notify_rule_row
        row = get_notify_rule_row(notify_type)
        if row is None:
            return False
        row = dict(row)
        if str(row.get("enabled", 1)) in ("0", "False", "false", "None"):
            return False
        return "bark" in _parse_channels(row.get("channels") or "[]")
    except Exception:
        return False


def set_bark_config(data: dict) -> None:
    """保存系统级 Bark 配置；掩码值不覆盖已存的 Device Key。"""
    if not isinstance(data, dict):
        return
    if "enabled" in data:
        cfg.config["bark_enabled"] = _bark_bool(data["enabled"], False)
    server = data.get("server")
    if isinstance(server, str) and server.strip():
        cfg.config["bark_server"] = server.strip().rstrip("/")
    key = data.get("device_key")
    if isinstance(key, str) and key.strip() and "****" not in key:
        cfg.config["bark_device_key"] = key.strip()
    for src, dst in (("sound", "bark_sound"), ("group", "bark_group")):
        if src in data and isinstance(data[src], str):
            cfg.config[dst] = data[src].strip()
    for src, dst in (("notify_library_new", "bark_notify_library_new"),
                     ("notify_request_new", "bark_notify_request_new")):
        if src in data:
            cfg.config[dst] = _bark_bool(data[src], True)
    cfg.save()


def get_library_notify_channels() -> str:
    return cfg.get("library_notify_channels", "")


def is_message_bot_notify_enabled() -> bool:
    return cfg.get("msg_bot_notify_enabled", True)


def is_message_bot_reply_enabled() -> bool:
    return cfg.get("msg_bot_reply_enabled", False)


def is_user_bot_configured() -> bool:
    return bool(cfg.get("tg_user_bot_token"))


def get_message_notification_base_url() -> str:
    return cfg.get("base_url", "").strip()


def get_notify_channels() -> str:
    return cfg.get("notify_channels", "")


def get_enable_library_notify() -> bool:
    return cfg.get("enable_library_notify", False)


def get_enable_notify() -> bool:
    return cfg.get("enable_notify", False)


def get_notify_user_login() -> bool:
    return cfg.get("notify_user_login", False)


def get_notify_item_deleted() -> bool:
    return cfg.get("notify_item_deleted", False)


def set_notify_user_login(enabled: bool) -> None:
    cfg.set("notify_user_login", enabled)


def set_notify_item_deleted(enabled: bool) -> None:
    cfg.set("notify_item_deleted", enabled)


def get_tg_bot_token() -> str:
    return cfg.get("tg_bot_token", "")


def get_wecom_corpid() -> str:
    return cfg.get("wecom_corpid", "")


def get_wecom_corpsecret() -> str:
    return cfg.get("wecom_corpsecret", "")


def get_wecom_agentid() -> str:
    return cfg.get("wecom_agentid", "")


def get_wecom_touser() -> str:
    return cfg.get("wecom_touser", "@all")


def get_pulse_url() -> str:
    return cfg.get("pulse_url", "")


def get_wecom_webhook() -> str:
    return cfg.get("wecom_webhook", cfg.get("wechat_webhook", ""))


def _get_notification_channels_config(*, masked: bool) -> dict:
    def mask_token(value):
        if not value or not isinstance(value, str):
            return value
        if len(value) <= 8:
            return "****"
        return value[:4] + "****" + value[-4:]

    return {
        "tg_bot": {
            "token": mask_token(cfg.get("tg_bot_token", "")) if masked else cfg.get("tg_bot_token", ""),
            "chat_id": cfg.get("tg_chat_id", ""),
            "enabled": cfg.get("enable_bot", False),
        },
        "tg_channels": json.loads(cfg.get("notify_channels", "[]")),
        "wecom": {
            "webhook": mask_token(cfg.get("wecom_webhook", cfg.get("wechat_webhook", ""))) if masked else cfg.get("wecom_webhook", cfg.get("wechat_webhook", "")),
            "enabled": cfg.get("enable_wecom", cfg.get("enable_wechat", False)),
        },
        # 系统级 Bark 通道（1.9.0）：Device Key 同样按 masked 做掩码
        "bark": get_bark_config(masked=masked),
    }


def get_notification_channels_config() -> dict:
    return _get_notification_channels_config(masked=True)


def get_notification_channels_runtime_config() -> dict:
    return _get_notification_channels_config(masked=False)


def get_wecom_runtime_config() -> dict:
    return {
        "corpid": cfg.get("wecom_corpid", ""),
        "corpsecret": cfg.get("wecom_corpsecret", ""),
        "agentid": cfg.get("wecom_agentid", ""),
        "touser": cfg.get("wecom_touser", "@all"),
    }


def get_notify_bot_runtime_config() -> dict:
    return {
        "tg_bot_token": cfg.get("tg_bot_token", ""),
        "tg_chat_id": cfg.get("tg_chat_id", ""),
        "wecom_corpid": cfg.get("wecom_corpid", ""),
    }


def set_notification_channels_config(data: dict) -> None:
    def should_update(value):
        if not value or not isinstance(value, str):
            return False
        return "****" not in value

    if "tg_bot" in data:
        token = data["tg_bot"].get("token", "")
        if should_update(token):
            cfg.config["tg_bot_token"] = token
        cfg.config["tg_chat_id"] = data["tg_bot"].get("chat_id", "")
        cfg.config["enable_bot"] = data["tg_bot"].get("enabled", False)

    if "tg_channels" in data:
        cfg.config["notify_channels"] = json.dumps(data["tg_channels"])

    if "wecom" in data:
        webhook = data["wecom"].get("webhook", "")
        if should_update(webhook):
            cfg.config["wecom_webhook"] = webhook
        cfg.config["enable_wecom"] = data["wecom"].get("enabled", False)

    if "bark" in data:
        set_bark_config(data["bark"])

    cfg.save()


def set_message_bot_notify_enabled(enabled: bool) -> None:
    cfg.set("msg_bot_notify_enabled", enabled)


def set_message_bot_reply_enabled(enabled: bool) -> None:
    cfg.set("msg_bot_reply_enabled", enabled)
