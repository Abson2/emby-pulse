# -*- coding: utf-8 -*-
"""全局 API 鉴权网关：默认拒绝，白名单放行。

背景
----
上游把鉴权分散写在每个路由函数里，漏写检查的接口即为匿名可访问
（社区通报的「二十四个接口直接放行」）。静态审计确认上游 482 个路由中
有 49 个不含任何鉴权信号，其中 /api 下写接口 6 个、读接口 11 个。

本中间件改为集中式白名单制：

* 除极少数必须匿名的入口外，所有 ``/api/*`` 请求必须携带有效会话
  （管理端 ``session["user"]`` / 用户端 ``session["req_user"]``）或有效
  ``Authorization: Bearer <API Token>``；
* 管理端路径额外要求管理员身份；
* 求片门户 / 统计类 GET 保留匿名只读（产品设定），但 POST/PUT/DELETE
  一律要求登录；可用 ``AUTH_GATE_STRICT=1`` 连只读也收紧；
* 未登录返回 401，已登录但权限不足返回 403，不再用「200 + status:error」
  这种容易被误判为成功的响应。

可调环境变量
------------
``AUTH_GATE_MODE``     ``enforce``(默认) / ``observe``(只告警放行) / ``off``(关闭)
``PUBLIC_API_PATHS``   逗号分隔，追加允许匿名访问的 ``/api`` 路径
``AUTH_GATE_STRICT=1`` 连公开只读数据也要求登录
"""

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# 必须匿名可访问的端点：认证入口、带独立凭证校验的回调、PWA 安装清单。
# 注意：/api/auth/settings、/api/pwa/config、/api/pwa/user_icon、
# /api/pwa/set_user_icon、/api/history/stats、/api/wallpaper 等
# 曾被发现「谁来都能进」，因此一律不放进白名单。
DEFAULT_PUBLIC_API_PATHS = (
    "/api/login",
    "/api/logout",
    "/api/register",
    "/api/requests/auth",
    "/api/requests/register",
    "/api/requests/check",
    "/api/v1/webhook",
    "/api/bot/webhook",
    "/api/bot/wecom_webhook",
    "/api/telegram/webhook",
    "/api/pwa/manifest.json",
)

# 管理端前缀：要求管理员身份（或 admin API Token）
ADMIN_ONLY_PREFIXES = (
    "/api/manage/",
    "/api/admin/",
    "/api/settings",
    "/api/bot/settings",
    "/api/tokens/",
    "/api/audit",
    "/api/diag/",
    "/api/system/",
    "/api/plugins/",
    "/api/tasks/",
    "/api/db/",
    "/api/clients/",
    "/api/risk/",
    "/api/notify/",
    "/api/proxy/clear_cache",
)

# 公开只读数据：仅为维持求片门户/首页的匿名浏览体验，写操作仍需登录
PUBLIC_READ_PREFIXES = (
    "/api/requests/",
    "/api/stats/",
)

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _matches(path: str, prefix: str) -> bool:
    return path == prefix.rstrip("/") or path.startswith(prefix)


def is_public_api(path: str) -> bool:
    extras = [
        p.strip()
        for p in os.getenv("PUBLIC_API_PATHS", "").split(",")
        if p.strip()
    ]
    return any(_matches(path, p) for p in list(DEFAULT_PUBLIC_API_PATHS) + extras)


def is_admin_api(path: str) -> bool:
    return any(_matches(path, p) for p in ADMIN_ONLY_PREFIXES)


class AuthGateMiddleware(BaseHTTPMiddleware):
    """放在 DatabaseSessionMiddleware 内层，以便读取 request.scope['session']。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if not path.startswith("/api/") or is_public_api(path):
            return await call_next(request)

        mode = os.getenv("AUTH_GATE_MODE", "enforce").strip().lower()
        if mode == "off":
            return await call_next(request)

        strict = os.getenv("AUTH_GATE_STRICT", "0") == "1"
        if (
            not strict
            and request.method in SAFE_METHODS
            and any(_matches(path, p) for p in PUBLIC_READ_PREFIXES)
            and not is_admin_api(path)
        ):
            return await call_next(request)

        session = request.scope.get("session") or {}
        admin_user = session.get("user")
        portal_user = session.get("req_user")
        is_admin = isinstance(admin_user, dict) and (
            admin_user.get("auth_type") == "emby"
            or admin_user.get("role") == "admin"
        )

        bearer_ok = False
        bearer_admin = False
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                from app.core.jwt_token import verify_api_token

                payload = verify_api_token(auth_header[7:].strip())
                if payload:
                    bearer_ok = True
                    bearer_admin = bool(payload.get("is_admin"))
            except Exception:
                pass

        logged_in = bool(admin_user or portal_user)
        allowed = (
            (is_admin or bearer_admin)
            if is_admin_api(path)
            else (logged_in or bearer_ok)
        )

        if allowed:
            return await call_next(request)

        # observe / warn 为同一灰度模式
        if mode in ("observe", "warn"):
            print("[AuthGate] WARN 未授权访问被放行: %s %s" % (request.method, path))
            return await call_next(request)

        return JSONResponse(
            status_code=401 if not logged_in else 403,
            content={"detail": "未登录" if not logged_in else "需要管理员权限"},
        )
