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

# 定义登录请求的数据模型
class LoginData(BaseModel):
    password: str

# -------------------------------------------------------------------------
# 核心鉴权逻辑
# -------------------------------------------------------------------------
def check_login(request: Request):
    token = request.cookies.get("access_token")
    correct_password = cfg.get("web_password")
    if not correct_password: return True
    if not token or token != correct_password: return False
    return True

# -------------------------------------------------------------------------
# API 接口 (登录/登出)
# -------------------------------------------------------------------------
@router.post("/api/login")
async def login_api(data: LoginData, response: Response):
    correct_password = cfg.get("web_password")
    if not correct_password:
        return JSONResponse(content={"status": "error", "msg": "系统未设置 web_password"})
    if data.password == correct_password:
        res = JSONResponse(content={"status": "success"})
        res.set_cookie(key="access_token", value=data.password, max_age=86400*30, httponly=True)
        return res
    else:
        return JSONResponse(content={"status": "error", "msg": "密码错误"})

@router.get("/logout")
async def logout(response: Response):
    res = RedirectResponse("/login")
    res.delete_cookie("access_token")
    return res

# -------------------------------------------------------------------------
# 页面路由 - 注册所有可能的路径
# -------------------------------------------------------------------------

# 1. 仪表盘 (Dashboard)
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

# 4. 数据洞察 (注册两个路径以防万一)
@router.get("/details", response_class=HTMLResponse)
@router.get("/data", response_class=HTMLResponse) 
async def details_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("details.html", {"request": request})

# 5. 映迹工坊 (报表)
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
@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("users.html", {"request": request})

# 8. 系统设置
@router.get("/system", response_class=HTMLResponse)
@router.get("/settings", response_class=HTMLResponse)
async def system_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("system.html", {"request": request})

# 9. 质量盘点 (Insight)
@router.get("/insight", response_class=HTMLResponse)
async def insight_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("insight.html", {"request": request})