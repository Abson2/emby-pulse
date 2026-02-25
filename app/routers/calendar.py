from fastapi import APIRouter, Request, Depends
from app.services.calendar_service import calendar_service
from app.core.config import templates

router = APIRouter()

@router.get("/calendar")
async def calendar_page(request: Request):
    """
    返回日历的前端页面 HTML
    """
    return templates.TemplateResponse("calendar.html", {"request": request, "page": "calendar"})

@router.get("/api/calendar/weekly")
async def get_weekly_calendar():
    """
    API: 获取本周数据 (JSON)
    """
    return calendar_service.get_weekly_calendar()