from fastapi import APIRouter, Request, Depends
from app.services.calendar_service import calendar_service
from app.core.config import templates

router = APIRouter()

@router.get("/calendar")
async def calendar_page(request: Request):
    """
    返回日历的前端页面 HTML
    """
    # 统一使用 active_page
    return templates.TemplateResponse("calendar.html", {"request": request, "active_page": "calendar"})

@router.get("/api/calendar/weekly")
async def get_weekly_calendar(refresh: bool = False, offset: int = 0): # <--- 增加 offset
    """
    API: 获取本周数据 (JSON)
    refresh: 是否强制刷新缓存
    offset: 周偏移 (0=本周, 1=下周, -1=上周)
    """
    return calendar_service.get_weekly_calendar(force_refresh=refresh, week_offset=offset)