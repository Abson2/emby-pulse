# -*- coding: utf-8 -*-
"""OpenAPI 文档访问控制（2.3）。

背景：2.1 之前 ``FastAPI(docs_url=None, redoc_url=None)`` 只关闭了文档页，
``/openapi.json`` 仍按 FastAPI 默认值开放；而 AuthGate 只拦截 ``/api/`` 前缀，
导致完整 API 结构实际可匿名读取。2.3 在启用文档的同时把三个路径一起纳入鉴权。

* 默认：仅管理员会话或 admin API Token 可访问；
* ``OPENAPI_PUBLIC=1``：显式匿名开放（供自家 AI / 内网工具直连，默认关闭）；
* 判定复用 ``auth_gate_middleware.is_admin_request``，语义与 API 网关一致。

本中间件必须放在 ``DatabaseSessionMiddleware`` 内层，才能读到会话。
"""
from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

OPENAPI_PATHS = frozenset({
    "/docs",
    "/redoc",
    "/openapi.json",
    "/docs/oauth2-redirect",
})


class OpenAPIGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path not in OPENAPI_PATHS:
            return await call_next(request)

        if os.getenv("OPENAPI_PUBLIC", "0").strip() == "1":
            return await call_next(request)

        from app.core.auth_gate_middleware import is_admin_request

        if is_admin_request(request):
            return await call_next(request)

        session = request.scope.get("session") or {}
        logged_in = bool(session.get("user") or session.get("req_user"))
        return JSONResponse(
            status_code=403 if logged_in else 401,
            content={"detail": "需要管理员权限" if logged_in else "未登录"},
        )
