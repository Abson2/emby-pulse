# -*- coding: utf-8 -*-
"""用户级通知设置 API（10308 用户端口）。

GET  /api/user/notify_settings  —— 读取当前用户通知设置
POST /api/user/notify_settings  —— 保存当前用户通知设置
"""
import logging
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.security_utils import safe_error_message
from app.domains.users import user_notify_service


logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api/user", tags=["用户通知设置"])


class UserNotifySettingsModel(BaseModel):
    channel: Optional[str] = "none"          # none | bark | webhook
    bark_url: Optional[str] = ""
    webhook_url: Optional[str] = ""
    notify_new_item: Optional[bool] = False
    notify_admin_message: Optional[bool] = False
    notify_announcement: Optional[bool] = False
    notify_wishlist: Optional[bool] = False


@router.get("/notify_settings")
def api_get_user_notify_settings(request: Request):
    user = request.session.get("req_user")
    if not user or not user.get("Id"):
        return {"status": "error", "message": "请先登录"}
    try:
        settings = user_notify_service.get_user_notify_settings(user["Id"])
        return {"status": "success", "data": settings}
    except Exception as e:
        return {"status": "error", "message": safe_error_message(e)}


@router.post("/notify_settings")
def api_save_user_notify_settings(data: UserNotifySettingsModel, request: Request):
    user = request.session.get("req_user")
    if not user or not user.get("Id"):
        return {"status": "error", "message": "请先登录"}
    try:
        # 🔒 SSRF 防护：用户自填的 Bark / Webhook 出站地址保存前即校验，禁止指向链路本地/回环
        if data.channel == "bark" and data.bark_url:
            server, _key = user_notify_service._parse_bark_url(data.bark_url)
            if server:
                from app.core.outbound_url import assert_safe_outbound
                assert_safe_outbound(server + "/")
        elif data.channel == "webhook" and data.webhook_url:
            from app.core.outbound_url import assert_safe_outbound
            assert_safe_outbound(data.webhook_url)
        user_notify_service.save_user_notify_settings(user["Id"], data.model_dump())
        return {"status": "success", "message": "通知设置已保存"}
    except ValueError as e:
        return {"status": "error", "message": f"通知地址不安全：{e}"}
    except Exception as e:
        return {"status": "error", "message": safe_error_message(e)}
