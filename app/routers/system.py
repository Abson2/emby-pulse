from fastapi import APIRouter, Request
from app.schemas.models import SettingsModel
from app.core.config import cfg, FALLBACK_IMAGE_URL, TMDB_FALLBACK_POOL
import requests
import random

router = APIRouter()

@router.get("/api/settings")
def api_get_settings(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    return {"status": "success", "data": cfg.get_all()}

@router.post("/api/settings")
def api_save_settings(data: SettingsModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    cfg.set("emby_host", data.emby_host.rstrip('/')); cfg.set("emby_api_key", data.emby_api_key)
    cfg.set("tmdb_api_key", data.tmdb_api_key); cfg.set("proxy_url", data.proxy_url); cfg.set("hidden_users", data.hidden_users)
    return {"status": "success"}

@router.get("/api/wallpaper")
def api_get_wallpaper():
    tmdb_key = cfg.get("tmdb_api_key"); proxy = cfg.get("proxy_url")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    if tmdb_key:
        try:
            url = f"https://api.themoviedb.org/3/trending/all/week?api_key={tmdb_key}&language=zh-CN"
            res = requests.get(url, timeout=8, proxies=proxies)
            if res.status_code == 200:
                data = res.json()
                results = [i for i in data.get("results", []) if i.get("backdrop_path")]
                if results:
                    target = random.choice(results)
                    return {"status": "success", "url": f"https://image.tmdb.org/t/p/original{target['backdrop_path']}", "title": target.get("title") or target.get("name")}
        except: pass
    return {"status": "success", "url": random.choice(TMDB_FALLBACK_POOL), "title": "Cinematic Collection"}