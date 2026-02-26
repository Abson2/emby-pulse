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
    return templates.TemplateResponse("calendar.html", {"request": request, "active_page": "calendar"})

@router.get("/api/calendar/weekly")
async def get_weekly_calendar(refresh: bool = False, offset: int = 0): 
    """
    API: 获取本周数据 (JSON)
    refresh: 是否强制刷新缓存
    offset: 周偏移 (0=本周, 1=下周, -1=上周)
    """
    return calendar_service.get_weekly_calendar(force_refresh=refresh, week_offset=offset)

@router.post("/api/calendar/config")
async def update_calendar_config(config: CalendarConfigReq):
    """
    API: 更新日历配置
    """
    cfg.set("calendar_cache_ttl", config.ttl)
    return {"status": "success"}