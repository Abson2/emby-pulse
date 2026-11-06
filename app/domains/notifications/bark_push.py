# -*- coding: utf-8 -*-
"""系统级 Bark 推送入口（1.9.0）
================================

职责：在 EMBYPULSE 系统层面提供一个独立的 Bark 推送通道，当前覆盖两类消息：

1. **新片入库通知**：`push_new_library_item()`（电影入库 / 剧集新集入库）
2. **新的求片通知**：`push_new_request()`（用户提交求片心愿）

与现有「通知规则渠道」的关系
----------------------------
Bark 的基础配置（服务器 / Device Key / 总开关 / 两类消息各自开关）由
`app/infra/config/notification_settings.py` 读写 config.json；
- 系统级两类事件（新片入库 / 新的求片）在各自触发点调用 `safe_push()`，
  由 Bark 自身开关控制；
- 🔥 1.9.7 起对齐企业微信「channels 列表驱动」：在「推送场景总控」或
  「工单大厅 / 用户管理通知设置」里勾选 bark，同样是合法启用来源
  （`is_bark_channel_selected()` 按通知类型精确判断，不再是不消费的死值）；
- 管理端 7 类通知规则事件通过 `safe_rule_push()` 走系统级 Bark 通道；
- TG / 企业微信 / Web 通知中心的逻辑完全不受影响。

配置边界：`WEBHOOK_TOKEN`（.env）只用于 Emby Webhook 回调地址 `?token=` 的
接口校验，属于「入库事件入口」，与本模块的推送出口配置没有任何耦合。

安全与可靠性约束
----------------
- 只使用 Python 标准库（urllib / json / logging / re），便于 CI 纯标准库测试；
- Device Key 属敏感信息：对外配置接口做掩码，本模块返回值里**不含** key；
- 推送失败只记日志并返回结构化结果，**绝不向业务主流程抛异常**；
- 未启用 / 未配置 Key / 服务器地址非法时直接跳过，不发请求；
- 支持 `transport` 注入，供回归测试在不联网的情况下验证真实请求体。

Bark 接口形态（POST JSON，兼容官方服务与自建服务器）：
    POST {server}/push
    {"device_key": "...", "title": "...", "body": "...",
     "sound": "...", "group": "...", "level": "active", "url": "..."}
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request

logger = logging.getLogger("uvicorn")

DEFAULT_BARK_GROUP = "EMBYPULSE"
DEFAULT_BARK_SOUND = "default"
DEFAULT_BARK_LEVEL = "active"
BARK_TIMEOUT = 10

_TITLE_MAX = 100
_BODY_MAX = 2000
_URL_MAX = 500
_SERVER_RE = re.compile(r"^https?://[^\s]+$", re.I)


def _as_bool(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _runtime_config(config=None):
    """取系统级 Bark 配置；允许测试/调用方直接注入 config。"""
    if isinstance(config, dict):
        return dict(config)
    try:
        from app.infra.config.notification_settings import get_bark_runtime_config
        return get_bark_runtime_config()
    except Exception as e:
        logger.warning(f"[Bark] 读取系统级配置失败: {e}")
        return {}


# 系统级两类事件 -> notify_rules 通知类型（用于按类型精确判断渠道勾选）
_RULE_TYPE_BY_KIND = {
    "library_new": "library_new",
    "request_new": "request_new",
}


def _apply_channel_gate(kind, conf):
    """🔥 1.9.7：把「推送场景总控 / 通知设置浮窗」里的 Bark 勾选折算为启用信号。

    借鉴企微由 channels 列表驱动的做法；按事件类型精确匹配，避免「工单大厅
    勾了 Bark」意外把「新片入库」也打开，也避免求片通知被重复触发。
    """
    notify_type = _RULE_TYPE_BY_KIND.get(kind)
    if not notify_type:
        return conf
    try:
        from app.infra.config.notification_settings import is_bark_channel_selected
        if is_bark_channel_selected(notify_type):
            conf["enabled"] = True
    except Exception:
        pass
    return conf


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text):
    """管理端通知文案多为 Telegram HTML，Bark 是纯文本，去掉标签避免乱码。"""
    return _HTML_TAG_RE.sub("", str(text or ""))


def _event_allowed(conf, switch_key):
    return (_as_bool(conf.get("enabled"), False)
            and _as_bool(conf.get(switch_key, True), True))


def send(title, body, *, url=None, group=None, sound=None, level=None,
         config=None, transport=None, force=False):
    """统一的 Bark 发送入口。返回 dict，任何失败都被吞掉，不抛给调用方。

    force=True 用于「测试推送」按钮：绕过总开关与两类消息开关，但仍要求
    已配置 Device Key 与合法服务器地址。
    """
    conf = _runtime_config(config)
    key = str(conf.get("device_key") or "").strip()
    server = str(conf.get("server") or "").strip().rstrip("/")

    # 🔥 1.9.7：跳过原因写进日志，用户可在系统日志里自证「为什么没推」
    if not force and not _as_bool(conf.get("enabled"), False):
        detail = "Bark 系统级推送未启用（卡片总开关与渠道勾选均未开启）"
        logger.info(f"[Bark] 未推送原因: {detail}")
        return {"ok": True, "skipped": True, "detail": detail}
    if not key:
        detail = "未配置 Bark Device Key（请在「机器人助手 → Bark 推送」保存，再点顶部保存配置）"
        logger.warning(f"[Bark] 未推送原因: {detail}")
        return {"ok": False, "skipped": True, "detail": detail}
    if not _SERVER_RE.match(server):
        detail = "Bark 服务器地址非法或未配置（需 http(s):// 开头）"
        logger.warning(f"[Bark] 未推送原因: {detail}")
        return {"ok": False, "skipped": True, "detail": detail}

    payload = {
        "device_key": key,
        # 🔥 Bark 是纯文本通道，不渲染 <b> 等 Telegram HTML 标签；
        # 在统一出口层剥离，根治「新片入库 / 用户级广播」推送出现 <b> 字面量的乱码。
        # 规则通知此前已在入口剥离，此处二次剥离幂等无副作用。
        "title": _strip_html(title)[:_TITLE_MAX],
        "body": _strip_html(body)[:_BODY_MAX],
        "sound": str(sound or conf.get("sound") or DEFAULT_BARK_SOUND),
        "group": str(group or conf.get("group") or DEFAULT_BARK_GROUP),
        "level": str(level or conf.get("level") or DEFAULT_BARK_LEVEL),
    }
    if url:
        payload["url"] = str(url)[:_URL_MAX]

    # 返回值里绝不携带 device_key，避免调用方误把整个结果写进日志
    safe_payload = {k: v for k, v in payload.items() if k != "device_key"}
    endpoint = server + "/push"

    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        opener = transport or urllib.request.urlopen
        with opener(req, timeout=BARK_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = int(getattr(resp, "status", 0) or resp.getcode() or 0)
        parsed = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {}
        code = parsed.get("code") if isinstance(parsed, dict) else None
        ok = 200 <= status < 300 and code in (None, 200)
        detail = "推送成功" if ok else f"Bark 返回 HTTP {status} / code={code}"
        if not ok:
            logger.warning(f"[Bark] {detail}")
        return {"ok": ok, "skipped": False, "detail": detail,
                "endpoint": endpoint, "payload": safe_payload}
    except Exception as e:
        logger.warning(f"[Bark] 推送异常（已忽略，不影响业务主流程）: {e}")
        return {"ok": False, "skipped": False,
                "detail": f"{type(e).__name__}: {e}",
                "endpoint": endpoint, "payload": safe_payload}


def push_new_library_item(*, name="", series_name="", episode_info="",
                         item_type="", year="", rating="", overview="",
                         play_url="", body=None, config=None, transport=None):
    """新片入库通知（电影 / 剧集新集共用）。

    若传入 ``body``（已由「自定义通知模板」插件渲染好的正文），则直接使用，
    不再内部拼装固定格式——保证 Bark 与管理端 TG / 企业微信 使用同一套风格模板。
    """
    conf = _runtime_config(config)
    if not _event_allowed(conf, "notify_library_new"):
        return {"ok": True, "skipped": True, "detail": "新片入库 Bark 通知未启用"}

    if body:
        return send("新片入库", str(body), url=play_url,
                    config=conf, transport=transport)

    display = str(series_name or name or "未知影片")
    suffix = f" {episode_info}" if episode_info else ""
    type_cn = ("剧集" if str(item_type).lower() in ("series", "episode", "tv", "show")
               else "电影")
    lines = [f"{display}{suffix}",
             "类型：" + type_cn + (f" · 年份：{year}" if year else "")]
    if rating and str(rating) not in ("N/A", "None", ""):
        lines.append(f"评分：{rating}")
    if overview:
        lines.append(str(overview)[:180])
    return send("新片入库", "\n".join(lines), url=play_url,
                config=conf, transport=transport)


def push_new_request(*, title="", year="", user="", media_type="",
                     seasons=None, admin_url="", config=None, transport=None):
    """新的求片通知（用户提交求片心愿时触发）。"""
    conf = _runtime_config(config)
    if not _event_allowed(conf, "notify_request_new"):
        return {"ok": True, "skipped": True, "detail": "新的求片 Bark 通知未启用"}

    season_nums = []
    for s in (seasons or []):
        try:
            n = int(s)
            if n > 0:
                season_nums.append(str(n))
        except (TypeError, ValueError):
            continue
    season_txt = (f"第 {','.join(season_nums)} 季"
                  if str(media_type) == "tv" and season_nums else "")
    type_txt = "剧集" if str(media_type) == "tv" else "电影"
    lines = [
        f"{title}" + (f" ({year})" if year else ""),
        f"用户：{user or '未知用户'}",
        "类型：" + type_txt + (f" · {season_txt}" if season_txt else ""),
        "请及时前往后台审批处理。",
    ]
    return send("新的求片", "\n".join(lines), url=admin_url,
                config=conf, transport=transport)


def send_test(*, config=None, transport=None):
    """管理端「测试推送」按钮：绕过开关，验证服务器与 Device Key 是否可用。"""
    conf = _runtime_config(config)
    return send(
        "EMBYPULSE 测试推送",
        "Bark 系统级推送配置成功。新片入库 / 新的求片通知将发送到这里。",
        config=conf, transport=transport, force=True,
    )


def safe_push(kind, **kwargs):
    """事件侧统一入口：任何异常都吞掉，绝不影响求片提交 / 入库通知主流程。"""
    try:
        # 🔥 1.9.7：真实运行路径（未显式注入 config）才读系统配置并把 channels
        # 勾选折算进启用判断；测试与用户级推送显式注入 config 时行为完全不变。
        if not isinstance(kwargs.get("config"), dict):
            kwargs["config"] = _apply_channel_gate(kind, _runtime_config())
        if kind == "library_new":
            return push_new_library_item(**kwargs)
        if kind == "request_new":
            return push_new_request(**kwargs)
        return {"ok": False, "skipped": True, "detail": f"未知推送类型: {kind}"}
    except Exception as e:
        logger.warning(f"[Bark] {kind} 推送异常（已忽略）: {e}")
        return {"ok": False, "skipped": True,
                "detail": f"{type(e).__name__}: {e}"}


def push_rule_notification(notify_type, title, body, *, url=None,
                           config=None, transport=None):
    """🔥 1.9.7：管理端通知规则渠道的 Bark 推送入口。

    用于「工单大厅通知设置」（request_new / request_update / request_status /
    feedback_new）与「用户管理通知设置」（user_register / user_login /
    user_delete）里勾选的 Bark 渠道。按 notify_type 精确判断，勾了才推，
    不是只保存不消费的假开关。

    渠道勾选本身即代表启用意图，因此不再额外要求 Bark 卡片总开关；但仍必须
    已配置 Device Key 与合法服务器地址，缺什么由 send() 明确写进日志。
    """
    try:
        if isinstance(config, dict):
            conf = dict(config)
        else:
            from app.infra.config.notification_settings import is_bark_channel_selected
            if not is_bark_channel_selected(notify_type):
                return {"ok": True, "skipped": True,
                        "detail": f"{notify_type} 未勾选 Bark 渠道"}
            conf = _runtime_config()
        conf["enabled"] = True
        return send(title, _strip_html(body), url=url,
                    config=conf, transport=transport)
    except Exception as e:
        logger.warning(f"[Bark] 通知规则 {notify_type} 推送异常（已忽略）: {e}")
        return {"ok": False, "skipped": True,
                "detail": f"{type(e).__name__}: {e}"}


def safe_rule_push(notify_type, title, body, **kwargs):
    """业务侧统一入口：内部已全量吞异常，调用方无需再包一层。"""
    res = push_rule_notification(notify_type, title, body, **kwargs)
    if isinstance(res, dict) and res.get("skipped"):
        logger.info(f"[Bark] 通知规则 {notify_type} 未推送: {res.get('detail')}")
    return res
