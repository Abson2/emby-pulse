from fastapi import APIRouter, Response
from fastapi.responses import RedirectResponse
from app.core.config import cfg, FALLBACK_IMAGE_URL
import requests

router = APIRouter()

@router.get("/api/proxy/image/{item_id}/{img_type}")
def proxy_image(item_id: str, img_type: str):
    return RedirectResponse(FALLBACK_IMAGE_URL) 

@router.get("/api/proxy/user_image/{user_id}")
def proxy_user_image(user_id: str, tag: str = None):
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if not key: return Response(status_code=404)
    try:
        url = f"{host}/emby/Users/{user_id}/Images/Primary?width=200&height=200&mode=Crop"
        if tag: url += f"&tag={tag}"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            headers = {"Cache-Control": "public, max-age=31536000", "Access-Control-Allow-Origin": "*"}
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"), headers=headers)
    except: pass
    return Response(status_code=404)