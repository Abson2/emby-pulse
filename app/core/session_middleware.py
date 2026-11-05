"""
Database Session Middleware
Replaces SessionMiddleware, no SECRET_KEY needed
"""
import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from app.core.session import session_manager, session_cookie_name


class DatabaseSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # v1.7.0：按端口选会话 Cookie 名。用户端(10308)的 wrapper 会在 scope 上
        # 打权威标记；管理端(10307) 无标记，用默认名。两端口各自独立会话，
        # 登录时的 session.clear() 不会再删掉另一端的会话行导致互踢。
        cookie_name = session_cookie_name(request.scope)
        session_id = request.cookies.get(cookie_name)
        session_dict = session_manager.get_or_create_session(session_id)
        # Set session in scope so request.session property works
        request.scope["session"] = session_dict
        if "csrf_token" not in session_dict:
            import secrets
            session_dict["csrf_token"] = secrets.token_urlsafe(32)
        response = await call_next(request)
        session_manager.save_modified()
        current_session_id = session_dict._session_id
        if current_session_id != session_id:
            # Detect if request is HTTPS
            is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            response.set_cookie(
                key=cookie_name,
                value=current_session_id,
                max_age=int(os.getenv("SESSION_ABSOLUTE_HOURS", "168")) * 3600,
                httponly=True,
                # lax 比 strict 更宽容：允许同站（如 10307↔10308、隧道↔局域网）导航携带，
                # 避免偶发的跨源导航导致 cookie 不被发送而被误判为未登录。
                samesite="lax",
                secure=is_https,  # Secure only on HTTPS
                path="/"
            )
        return response
