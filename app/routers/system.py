from fastapi import APIRouter, Request
from app.schemas.models import SettingsModel
from app.core.config import cfg, save_config
import requests

router = APIRouter()

@router.get("/api/settings")
def api_get_settings(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    return {
        "status": "success",
        "data": {
            "emby_host": cfg.get("emby_host"),
            "emby_api_key": cfg.get("emby_api_key"),
            "tmdb_api_key": cfg.get("tmdb_api_key"),
            "proxy_url": cfg.get("proxy_url"),
            "webhook_token": cfg.get("webhook_token", "embypulse"),
            "hidden_users": cfg.get("hidden_users") or [],
            # 🔥 返回新字段
            "emby_public_url": cfg.get("emby_public_url", ""),
            "welcome_message": cfg.get("welcome_message", ""),
            # 🔥 新增返回字段
            "client_download_url": cfg.get("client_download_url", "")
        }
    }

@router.post("/api/settings")
def api_update_settings(data: SettingsModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    
    # 验证 Emby 连接
    try:
        res = requests.get(f"{data.emby_host}/emby/System/Info?api_key={data.emby_api_key}", timeout=5)
        if res.status_code != 200:
            return {"status": "error", "message": "无法连接 Emby，请检查地址或 API Key"}
    except:
        return {"status": "error", "message": "Emby 地址无法访问"}

    # 更新配置
    cfg["emby_host"] = data.emby_host
    cfg["emby_api_key"] = data.emby_api_key
    cfg["tmdb_api_key"] = data.tmdb_api_key
    cfg["proxy_url"] = data.proxy_url
    cfg["webhook_token"] = data.webhook_token
    cfg["hidden_users"] = data.hidden_users
    # 🔥 保存新字段
    cfg["emby_public_url"] = data.emby_public_url
    cfg["welcome_message"] = data.welcome_message
    # 🔥 新增保存逻辑
    cfg["client_download_url"] = data.client_download_url
    
    save_config()
    
    return {"status": "success", "message": "配置已保存"}

@router.post("/api/settings/test_tmdb")
def api_test_tmdb(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    data = request.json() # 获取body里的 api_key 等
    # 这里其实直接读 cfg 也可以，但前端可能发过来测试
    # 简化逻辑，直接测试 cfg 里的
    tmdb_key = cfg.get("tmdb_api_key")
    proxy = cfg.get("proxy_url")
    
    if not tmdb_key: return {"status": "error", "message": "未配置 TMDB API Key"}
    
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        url = f"https://api.themoviedb.org/3/authentication/token/new?api_key={tmdb_key}"
        res = requests.get(url, proxies=proxies, timeout=10)
        if res.status_code == 200:
            return {"status": "success", "message": "TMDB 连接成功"}
        return {"status": "error", "message": f"连接失败: {res.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}