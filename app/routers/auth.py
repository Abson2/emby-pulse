from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from app.core.config import templates, cfg
from app.schemas.models import LoginModel
import requests

router = APIRouter()

@router.get("/login")
async def page_login(request: Request):
    if request.session.get("user"): return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request})

@router.get("/logout")
async def api_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")

@router.post("/api/login")
def api_login(data: LoginModel, request: Request):
    try:
        host = cfg.get("emby_host")
        if not host: return {"status": "error", "message": "请配置 EMBY_HOST"}
        res = requests.post(f"{host}/emby/Users/AuthenticateByName", json={"Username": data.username, "Pw": data.password}, headers={"X-Emby-Authorization": 'MediaBrowser Client="EmbyPulse", Device="Web", DeviceId="EmbyPulse", Version="1.0.0"'}, timeout=5)
        if res.status_code == 200:
            user_info = res.json().get("User", {})
            if not user_info.get("Policy", {}).get("IsAdministrator", False): return {"status": "error", "message": "仅限管理员"}
            request.session["user"] = {"id": user_info.get("Id"), "name": user_info.get("Name"), "is_admin": True}
            return {"status": "success"}
        else: return {"status": "error", "message": "验证失败"}
    except Exception as e: return {"status": "error", "message": str(e)}