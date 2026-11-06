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
        user_notify_service.save_user_notify_settings(user["Id"], data.model_dump())
        return {"status": "success", "message": "通知设置已保存"}
    except Exception as e:
        return {"status": "error", "message": safe_error_message(e)}
