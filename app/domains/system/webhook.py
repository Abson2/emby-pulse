from fastapi import APIRouter, Request, HTTPException
from app.domains.system.client_dao import list_client_blacklist_names, list_client_whitelist_user_ids
from app.domains.system.webhook_playback_dao import save_webhook_playback_ip_data
# 🔥 引入事件总线
from app.core.event_bus import bus
from app.infra.clients.media_server_client import media_api
from app.infra.config.bot_settings import get_webhook_token
import json
import logging
import os
import secrets
import ipaddress

logger = logging.getLogger("uvicorn")
router = APIRouter()

# 🔒 Webhook payload 上限（1MB），防止 DoS
MAX_WEBHOOK_PAYLOAD = 1024 * 1024


def _get_webhook_token(request: Request):
    """读取 Webhook Token：请求头优先，默认兼容 URL 参数 ?token=。

    Emby / Jellyfin 的 Webhook 插件只能配置一个回调 URL，无法附带自定义
    请求头，因此 1.9.0 起默认兼容 query 形式：

        http://<host>:10307/api/v1/webhook?token=<WEBHOOK_TOKEN>

    安全边界：
    - 若反代能注入请求头，仍优先使用 X-Webhook-Token；
    - 需要完全禁止 URL 凭证时，显式设置 WEBHOOK_ALLOW_QUERY_TOKEN=0
      即可回到严格的 Header-only 模式。
    - 本部署的 Webhook 由 Emby 服务端在**内网直连**发起，不经过浏览器与
      公网反代，因此没有浏览器历史 / Referer / 公网日志泄露面；凭证只会
      留在 EMBYPULSE 本机访问日志。若日后把 10307 暴露到公网或改走公网
      反代，再收紧日志采集或切回 Header-only。
    """
    token = request.headers.get("X-Webhook-Token")
    if token:
        return token
    # 默认 "1"：兼容 Emby Webhook 插件；仅显式设为 "0" 才回到严格模式
    if os.getenv("WEBHOOK_ALLOW_QUERY_TOKEN", "1") != "0":
        return request.query_params.get("token")
    return None


def _save_playback_ip_data(data, user_id, user_name, item, ip):
    """保存播放 IP 信息到本地数据库"""
    try:
        save_webhook_playback_ip_data(data, user_id, user_name, item, ip)
    except Exception as e:
        logger.error(f"[Webhook] 保存 IP 信息失败: {e}")


def intercept_illegal_client(data: dict):
    session = data.get("Session", {})
    device_id = session.get("DeviceId") or data.get("DeviceId")
    client = session.get("Client") or data.get("Client") or data.get("AppName")
    session_id = session.get("Id")
    
    # 🔥 获取当前用户 ID
    user = data.get("User") or session
    user_id = user.get("Id") or data.get("UserId")
    
    if not client or not device_id:
        return False
        
    client_lower = client.lower()
    try:
        blacklist_rows = list_client_blacklist_names()
        if not blacklist_rows: return False
            
        blacklist = [r['app_name'].lower() for r in blacklist_rows]
        if client_lower in blacklist:
            # 🔥 检查是否为白名单用户
            whitelist_rows = list_client_whitelist_user_ids()
            whitelist_user_ids = set(r['user_id'] for r in whitelist_rows) if whitelist_rows else set()
            if user_id and user_id in whitelist_user_ids:
                logger.info(f"[白名单跳过] 用户 {user.get('Name', user_id)} 在白名单中，允许使用 {client}")
                return False  # 白名单用户不拦截
            if session_id:
                msg_cmd = {
                    "Name": "DisplayMessage",
                    "Arguments": {
                        "Header": "🚫 违规客户端拦截",
                        "Text": f"检测到违规客户端 ({client})，该设备已被踢出！",
                        "TimeoutMs": "10000"
                    }
                }
                try: media_api.post(f"/Sessions/{session_id}/Command", json=msg_cmd, timeout=2)
                except Exception: pass
                try: media_api.post(f"/Sessions/{session_id}/Playing/Stop", timeout=2)
                except Exception: pass
            
            try: media_api.delete("/Devices", params={"Id": device_id}, timeout=3)
            except Exception: pass
            
            logger.warning(f"💥 [主动防御] 已秒踢违规客户端: {client}")
            return True
    except Exception: pass
    return False

@router.post("/api/v1/webhook")
async def emby_webhook(request: Request):
    # 🔒 Payload size 上限（防 DoS）
    cl = request.headers.get("content-length")
    try:
        if cl is not None and int(cl) > MAX_WEBHOOK_PAYLOAD:
            raise HTTPException(status_code=413, detail="Payload too large")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Content-Length")

    # 🔒 安全：Header 优先；Emby Webhook 插件可用 URL 参数 ?token=xxx
    token = _get_webhook_token(request)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="缺少 Webhook Token：请配置 X-Webhook-Token 请求头，"
                   "或在回调 URL 中使用 ?token=<Token>",
        )

    # 🔒 常量时间比对，防止时序攻击
    expected = get_webhook_token() or ""
    if not expected or not secrets.compare_digest(str(token), str(expected)):
        raise HTTPException(status_code=403, detail="Invalid Token")

    try:
        data = None
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
        elif "form" in content_type:
            form = await request.form()
            raw_data = form.get("data")
            if raw_data: data = json.loads(raw_data)

        if not data: return {"status": "error", "message": "Empty"}

        # 1. 违规拦截（最高优先级）
        if intercept_illegal_client(data):
            return {"status": "success", "message": "Blocked"}

        # 🔥 2. 定时检查黑名单设备（防止漏网之鱼）
        try:
            from app.domains.system.clients import check_and_block_blacklist_devices
            check_and_block_blacklist_devices()
        except Exception as e:
            logger.debug(f"[定时检查黑名单] 跳过: {e}")

        event = data.get("Event", "").lower().strip()
        # 只对重要事件输出日志，减少刷屏
        important_events = ["playback.start", "playback.stop", "item.added", "library.new", "auth", "login", "delete", "remove"]
        if event and any(e in event for e in important_events):
            logger.info(f"🔔 触发事件: {event}")

        # 3. 播放开始/停止事件立即保存 IP 信息（不依赖 enable_notify）
        if event in ["playback.start", "playback.stop"]:
            try:
                session = data.get("Session") or data
                item = data.get("Item") or session.get("NowPlayingItem") or {}
                user = data.get("User") or session
                user_id = user.get("Id") or data.get("UserId")
                user_name = user.get("Name") or user.get("UserName") or "未知用户"
                ip = session.get("RemoteEndPoint") or data.get("RemoteEndPoint") or ""
                # 智能处理 IPv4/IPv6 端口号
                if ip:
                    try:
                        ip_obj = ipaddress.ip_address(ip.split(',')[0].split(':')[0] if ',' in ip else ip.split(':')[0])
                        if ip_obj.version == 4 and ip.count(':') == 1:
                            # IPv4 带端口: 192.168.1.1:8080
                            ip = ip.rsplit(':', 1)[0]
                        elif ip_obj.version == 6:
                            # IPv6: 检查是否带端口 (如 [::1]:8080 或 ::1:8080)
                            if ip.startswith('['):
                                ip = ip.split(']')[0][1:]  # 去掉 [ ]
                            # IPv6 地址保持原样
                    except:
                        pass
                if user_id and user_name and item.get('Id'):
                    _save_playback_ip_data(data, user_id, user_name, item, ip)
            except Exception as e:
                logger.error(f"[Webhook] 保存 IP 数据异常: {e}")

        # 2. 彻底解耦：不再调 bot，而是发布到事件总线
        bus.publish("webhook.received", event, data)

        return {"status": "success"}
    except Exception as e:
        logger.error(f"Webhook 异常: {e}")
        return {"status": "error", "message": "Webhook 处理失败"}
