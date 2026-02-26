from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel
from app.services.calendar_service import calendar_service
from app.core.config import templates, cfg

router = APIRouter()

# 定义请求模型
class CalendarConfigReq(BaseModel):
    ttl: int

@router.get("/calendar")
async def calendar_page(request: Request):
    """
    返回日历的前端页面 HTML
    """
    if not request.session.get("user"):
        return templates.TemplateResponse("login.html", {"request": request})
        
    # 🔥 获取公网地址，如果没有则使用内网地址作为回退
    public_url = cfg.get("emby_public_url") or cfg.get("emby_public_host") or cfg.get("emby_host")
    if public_url and public_url.endswith('/'): public_url = public_url[:-1]

    return templates.TemplateResponse("calendar.html", {
        "request": request, 
        "user": request.session.get("user"), 
        "active_page": "calendar",
        "emby_public_url": public_url # 注入变量
    })

# 🔥🔥 核心修复：去掉 async 和 await
@router.get("/api/calendar/weekly")
def get_weekly_calendar(refresh: bool = False, offset: int = 0): 
    """
    API: 获取本周数据 (JSON)
    refresh: 是否强制刷新缓存
    offset: 周偏移 (0=本周, 1=下周, -1=上周)
    """
    # calendar_service.get_weekly_calendar 是同步方法，不能用 await
    return calendar_service.get_weekly_calendar(force_refresh=refresh, week_offset=offset)

@router.post("/api/calendar/config")
async def update_calendar_config(config: CalendarConfigReq):
    """
    API: 更新日历配置
    """
    cfg.set("calendar_cache_ttl", config.ttl)
    return {"status": "success"}