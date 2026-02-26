from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from app.core.config import templates, cfg
from app.services.calendar_service import CalendarService
import json

router = APIRouter()
cal_service = CalendarService()

@router.get("/calendar", response_class=HTMLResponse)
async def view_calendar(request: Request):
    if not request.session.get("user"):
        return templates.TemplateResponse("login.html", {"request": request})
    
    # 🔥 获取公网地址，如果没有则使用内网地址作为回退
    public_url = cfg.get("emby_public_url") or cfg.get("emby_host")
    if public_url and public_url.endswith('/'): public_url = public_url[:-1]

    return templates.TemplateResponse("calendar.html", {
        "request": request, 
        "user": request.session.get("user"), 
        "active_page": "calendar",
        "emby_public_url": public_url # 注入变量
    })

@router.get("/api/calendar/data")
async def api_calendar_data(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    
    # 强制刷新参数
    refresh = request.query_params.get("refresh") == "true"
    
    data = await cal_service.get_calendar_data(force_refresh=refresh)
    return {"status": "success", "data": data}

@router.post("/api/calendar/clear_cache")
def api_clear_calendar_cache(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    cal_service.clear_cache()
    return {"status": "success", "message": "缓存已清除"}