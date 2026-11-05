"""
Database Session Middleware
Replaces SessionMiddleware, no SECRET_KEY needed
"""
import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from app.core.session import session_manager, SESSION_COOKIE_NAME


class DatabaseSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
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
                key=SESSION_COOKIE_NAME,
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
