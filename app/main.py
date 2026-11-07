import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.bootstrap.database import initialize_databases
from app.bootstrap.lifespan import build_lifespan
from app.bootstrap.logging import configure_sensitive_log_filter
from app.bootstrap.middleware import configure_middlewares
from app.bootstrap.routes import register_routes, register_static_assets
from app.bootstrap.runtime import (
    clear_system_sessions,
    ensure_runtime_directories,
    patch_sqlite_connect,
)
from app.core.config import PORT
from app.core.rate_limiter import initialize_trusted_proxies, start_cleanup_timer
from app.shared.version import APP_VERSION

REQUEST_PORT = int(os.getenv("REQUEST_PORT", "10308"))


def prepare_runtime() -> None:
    configure_sensitive_log_filter()
    initialize_trusted_proxies()
    start_cleanup_timer()
    patch_sqlite_connect()
    print("[🔧 SQLite] 已启用 WAL 模式 + 30秒超时（解决 database is locked）")

    ensure_runtime_directories()
    clear_system_sessions()

    try:
        from app.core.security_check import run_security_checks

        run_security_checks()
    except Exception as e:
        print(f"[🔒 安全] 安全检查失败: {e}")

    initialize_databases()


def create_app() -> FastAPI:
    prepare_runtime()
    app = FastAPI(
        lifespan=build_lifespan(REQUEST_PORT),
        title="EmbyPulse API",
        description="EmbyPulse 开放接口与插件开发契约；/docs、/redoc、/openapi.json 默认仅管理员可访问。",
        version=APP_VERSION,
        # /docs 与 /redoc 由 configure_openapi_docs() 自定义提供，为内联脚本注入
        # 每请求的 CSP nonce；openapi.json 由 FastAPI 自动生成。
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    configure_exception_handlers(app)
    configure_middlewares(app)
    register_static_assets(app)
    register_routes(app)
    configure_openapi_docs(app)
    return app


def configure_openapi_docs(app: FastAPI) -> None:
    """2.3：启用带 CSP nonce 的 Swagger UI / ReDoc。

    FastAPI 内置文档页含无 nonce 的内联 <script>，会被本项目
    ``script-src-elem`` 的 nonce 策略拦截；此处生成同一页面并注入 nonce。
    """
    from fastapi import Request
    from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
    from fastapi.responses import HTMLResponse

    def _with_nonce(response, request: Request):
        nonce = getattr(request.state, "csp_nonce", "")
        body = response.body.decode("utf-8").replace(
            "<script>", '<script nonce="%s">' % nonce
        )
        return HTMLResponse(body, status_code=response.status_code)

    @app.get("/docs", include_in_schema=False)
    async def _swagger_ui(request: Request):
        return _with_nonce(
            get_swagger_ui_html(openapi_url=app.openapi_url, title=app.title + " - Swagger UI"),
            request,
        )

    @app.get("/redoc", include_in_schema=False)
    async def _redoc(request: Request):
        return _with_nonce(
            get_redoc_html(openapi_url=app.openapi_url, title=app.title + " - ReDoc"),
            request,
        )


def configure_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def _global_exception_handler(request: Request, exc: Exception):
        if isinstance(exc, StarletteHTTPException):
            raise exc

        import logging
        import traceback
        import uuid

        request_id = uuid.uuid4().hex[:12]
        logging.getLogger("app.unhandled").error(
            f"[未捕获异常] request_id={request_id} path={request.url.path} "
            f"method={request.method}\n{traceback.format_exc()}"
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "request_id": request_id},
        )


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
