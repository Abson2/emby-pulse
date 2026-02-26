from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from app.core.config import cfg
from app.core.database import query_db
import logging

# 初始化
logger = logging.getLogger("uvicorn")
templates = Jinja2Templates(directory="templates")

router = APIRouter()

# -------------------------------------------------------------------------
# 核心鉴权逻辑 (回归 Session 模式)
# -------------------------------------------------------------------------
def check_login(request: Request):
    """
    检查 Session 中是否有用户信息
    """
    user = request.session.get("user")
    if user and user.get("is_admin"):
        return True
    return False

# -------------------------------------------------------------------------
# 页面路由
# -------------------------------------------------------------------------

# 1. 仪表盘
@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("index.html", {"request": request, "active_page": "dashboard"})

# 2. 登录页
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if check_login(request): return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request})

# 🔥 新增：邀请注册页
@router.get("/invite/{code}", response_class=HTMLResponse)
async def invite_page(code: str, request: Request):
    # 校验邀请码有效性
    invite = query_db("SELECT * FROM invitations WHERE code = ?", (code,), one=True)
    valid = False
    days = 0
    if invite and invite['used_count'] < invite['max_uses']:
        valid = True
        days = invite['days']
    
    return templates.TemplateResponse("register.html", {"request": request, "code": code, "valid": valid, "days": days})

# 3. 内容排行
@router.get("/content", response_class=HTMLResponse)
async def content_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("content.html", {"request": request, "active_page": "content"})

# 4. 数据洞察
@router.get("/details", response_class=HTMLResponse)
async def details_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("details.html", {"request": request, "active_page": "details"})

# 5. 映迹工坊
@router.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("report.html", {"request": request, "active_page": "report"})

# 6. 机器人助手
@router.get("/bot", response_class=HTMLResponse)
async def bot_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("bot.html", {"request": request, "active_page": "bot"})

# 7. 用户管理
@router.get("/users_manage", response_class=HTMLResponse)
@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("users.html", {"request": request, "active_page": "users"})

# 8. 系统设置
@router.get("/settings", response_class=HTMLResponse)
@router.get("/system", response_class=HTMLResponse)
async def system_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("settings.html", {"request": request, "active_page": "settings"})

# 9. 质量盘点
@router.get("/insight", response_class=HTMLResponse)
async def insight_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("insight.html", {"request": request, "active_page": "insight"})

# 10. 任务中心
@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    if not check_login(request): return RedirectResponse("/login")
    return templates.TemplateResponse("tasks.html", {"request": request, "active_page": "tasks"})

# 11.历史记录
@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("history.html", {"request": request, "user": user, "active_page": "history"})