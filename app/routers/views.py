from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from app.core.config import cfg
import logging

# 初始化日志和模版
logger = logging.getLogger("uvicorn")
templates = Jinja2Templates(directory="templates")

router = APIRouter()

# 对应前端 login.html 发送的 JSON 结构
class LoginPayload(BaseModel):
    username: str
    password: str

# -------------------------------------------------------------------------
# 核心鉴权逻辑 (Session 优先)
# -------------------------------------------------------------------------
def check_login(request: Request):
    """
    检查用户是否已登录
    优先检查 Session (API通用)，兼容检查 Cookie (页面通用)
    """
    # 1. 检查 Session (最准)
    user_session = request.session.get("user")
    if user_session:
        return True
        
    # 2. 检查 Cookie (作为兜底，防止 Session 过期但 Cookie 还在)
    token = request.cookies.get("access_token")
    correct_password = cfg.get("web_password")
    
    if not correct_password: return True # 未设密码则放行
    if token and token == correct_password:
        # 如果 Cookie 对，自动补上 Session
        request.session["user"] = {"name": "Admin", "is_admin": True}
        return True
        
    return False

# -------------------------------------------------------------------------
# API 接口 (登录/登出)
# -------------------------------------------------------------------------
@router.post("/api/login")
async def login_api(data: LoginPayload, request: Request, response: Response):
    """
    登录：同时写入 Session 和 Cookie，确保万无一失
    """
    correct_password = cfg.get("web_password")
    
    # 允许密码为空的情况（如果配置没写）
    if not correct_password:
        correct_password = "" 

    # 简单验证密码 (忽略用户名，因为是单用户系统)
    if data.password == correct_password:
        # 1. 写入 Session (给 API 用)
        request.session["user"] = {"name": data.username or "Admin", "is_admin": True}
        
        # 2. 写入 Cookie (给页面跳转用，有效期 30 天)
        res = JSONResponse(content={"status": "success"})
        res.set_cookie(key="access_token", value=data.password, max_age=86400*30, httponly=True)
        return res
    else:
        return JSONResponse(content={"status": "error", "msg": "密码错误"})

@router.get("/logout")
async def logout(request: Request, response: Response):
    """
    退出：彻底清理所有状态
    """
    # 1. 清理 Session
    request.session.clear()
    
    # 2. 清理 Cookie
    res = RedirectResponse("/login", status_code=302)
    res.delete_cookie("access_token")
    return res

# -------------------------------------------------------------------------
# 页面路由
# -------------------------------------------------------------------------

# 1. 仪表盘
@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("index.html", {"request": request})

# 2. 登录页
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if check_login(request): return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request})

# 3. 内容排行
@router.get("/content", response_class=HTMLResponse)
async def content_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("content.html", {"request": request})

# 4. 数据洞察
@router.get("/details", response_class=HTMLResponse)
async def details_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("details.html", {"request": request})

# 5. 映迹工坊
@router.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("report.html", {"request": request})

# 6. 机器人助手
@router.get("/bot", response_class=HTMLResponse)
async def bot_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("bot.html", {"request": request})

# 7. 用户管理
@router.get("/users_manage", response_class=HTMLResponse)
@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("users.html", {"request": request})

# 8. 系统设置
@router.get("/settings", response_class=HTMLResponse)
@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("settings.html", {"request": request})

# 9. 质量盘点
@router.get("/insight", response_class=HTMLResponse)
async def insight_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("insight.html", {"request": request})

# 10. 任务中心
@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("tasks.html", {"request": request})