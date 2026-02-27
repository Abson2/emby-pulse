import os
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.core.config import cfg
from app.core.database import query_db
import logging

logger = logging.getLogger("uvicorn")
templates = Jinja2Templates(directory="templates")
router = APIRouter()

# 🔥 获取应用版本号 (没读到就显示开发版)
APP_VERSION = os.environ.get("APP_VERSION", "1.2.0.Dev")

def check_login(request: Request):
    user = request.session.get("user")
    if user and user.get("is_admin"):
        return True
    return False

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("index.html", {"request": request, "active_page": "dashboard", "version": APP_VERSION})

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if check_login(request): return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request, "version": APP_VERSION})

@router.get("/invite/{code}", response_class=HTMLResponse)
async def invite_page(code: str, request: Request):
    invite = query_db("SELECT * FROM invitations WHERE code = ?", (code,), one=True)
    valid = False; days = 0
    if invite and invite['used_count'] < invite['max_uses']:
        valid = True; days = invite['days']
    
    # 🔥 获取自定义下载链接 (没填则使用默认)
    client_url = cfg.get("client_download_url") or "https://emby.media/download.html"
    
    return templates.TemplateResponse("register.html", {
        "request": request, "code": code, "valid": valid, "days": days, 
        "client_download_url": client_url, "version": APP_VERSION
    })

@router.get("/content", response_class=HTMLResponse)
async def content_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("content.html", {"request": request, "active_page": "content", "version": APP_VERSION})

@router.get("/details", response_class=HTMLResponse)
async def details_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("details.html", {"request": request, "active_page": "details", "version": APP_VERSION})

@router.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("report.html", {"request": request, "active_page": "report", "version": APP_VERSION})

@router.get("/bot", response_class=HTMLResponse)
async def bot_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("bot.html", {"request": request, "active_page": "bot", "version": APP_VERSION})

@router.get("/users_manage", response_class=HTMLResponse)
@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("users.html", {"request": request, "active_page": "users", "version": APP_VERSION})

@router.get("/settings", response_class=HTMLResponse)
@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("settings.html", {"request": request, "active_page": "settings", "version": APP_VERSION})

@router.get("/insight", response_class=HTMLResponse)
async def insight_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("insight.html", {"request": request, "active_page": "insight", "version": APP_VERSION})

@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("tasks.html", {"request": request, "active_page": "tasks", "version": APP_VERSION})

@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    user = request.session.get("user")
    if not user: return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("history.html", {"request": request, "user": user, "active_page": "history", "version": APP_VERSION})