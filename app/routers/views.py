from fastapi import APIRouter, Request, HTTPException, Response
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
    """
    检查用户是否已登录 (验证 Cookie)
    """
    token = request.cookies.get("access_token")
    correct_password = cfg.get("web_password")
    
    # 如果配置文件里没设密码，默认允许通过（或者你需要强制设置密码）
    if not correct_password:
        return True
        
    if not token or token != correct_password:
        return False
    return True

# -------------------------------------------------------------------------
# 登录 API (之前可能缺了这个，导致登录点不动)
# -------------------------------------------------------------------------
@router.post("/api/login")
async def login_api(data: LoginData, response: Response):
    """
    处理登录请求，写入 Cookie
    """
    correct_password = cfg.get("web_password")
    
    if not correct_password:
        return JSONResponse(content={"status": "error", "msg": "系统未设置 web_password，请检查配置文件"})

    if data.password == correct_password:
        # 登录成功
        res = JSONResponse(content={"status": "success"})
        # 写入 Cookie，有效期 30 天
        res.set_cookie(key="access_token", value=data.password, max_age=86400*30, httponly=True)
        return res
    else:
        # 登录失败
        return JSONResponse(content={"status": "error", "msg": "密码错误"})

@router.get("/logout")
async def logout(response: Response):
    """
    退出登录
    """
    res = RedirectResponse("/login")
    res.delete_cookie("access_token")
    return res

# -------------------------------------------------------------------------
# 页面路由
# -------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("index.html", {"request": request})

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # 如果已经登录，直接跳到首页
    if check_login(request): return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request})

@router.get("/content", response_class=HTMLResponse)
async def content_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("content.html", {"request": request})

@router.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("report.html", {"request": request})

@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("users.html", {"request": request})

# 质量盘点页面
@router.get("/insight", response_class=HTMLResponse)
async def insight_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("insight.html", {"request": request})