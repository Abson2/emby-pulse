from fastapi import APIRouter, Response
from app.core.config import cfg, FALLBACK_IMAGE_URL
import requests
from functools import lru_cache

router = APIRouter()

# 🔥 新增：简单的内存缓存，避免重复查询 API 拖慢速度
@lru_cache(maxsize=2000)
def get_real_image_id(item_id: str):
    """
    智能判断：如果是单集 (Episode)，尝试向上寻找剧集 ID (SeriesId)
    这样能获取到竖屏海报，而不是横屏剧照
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    if not key or not host: return item_id

    try:
        url = f"{host}/emby/Items/{item_id}?api_key={key}"
        res = requests.get(url, timeout=2)
        if res.status_code == 200:
            data = res.json()
            # 如果是单集，且有 SeriesId，则返回 SeriesId
            if data.get("Type") == "Episode" and data.get("SeriesId"):
                return data.get("SeriesId")
            # 如果是季，也返回 SeriesId
            if data.get("Type") == "Season" and data.get("SeriesId"):
                return data.get("SeriesId")
    except:
        pass
    # 其他情况（电影、或者查询失败）直接返回原 ID
    return item_id

@router.get("/api/proxy/image/{item_id}/{img_type}")
def proxy_image(item_id: str, img_type: str):
    """
    代理 Emby 的图片资源
    集成智能海报替换功能
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    if not key or not host:
        return Response(status_code=404)

    try:
        # 🔥 核心逻辑：智能转换 ID
        # 只有请求 Primary (封面) 时才尝试转换，Backdrop (背景) 还是用单集的剧照比较合适
        target_id = item_id
        if img_type.lower() == 'primary':
            target_id = get_real_image_id(item_id)

        # 构造 Emby 图片 URL
        url = f"{host}/emby/Items/{target_id}/Images/{img_type}?maxHeight=600&maxWidth=400&quality=90&api_key={key}"
        
        # 发起请求
        resp = requests.get(url, timeout=10, stream=True)
        
        if resp.status_code == 200:
            # 透传图片内容和 Content-Type
            return Response(
                content=resp.content, 
                media_type=resp.headers.get("Content-Type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"} # 缓存1天
            )
    except Exception as e:
        print(f"Proxy Image Error: {e}")
        pass
        
    # 失败则重定向到默认图
    return Response(status_code=404)

@router.get("/api/proxy/user_image/{user_id}")
def proxy_user_image(user_id: str, tag: str = None):
    """
    代理用户头像
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    if not key: 
        return Response(status_code=404)
        
    try:
        url = f"{host}/emby/Users/{user_id}/Images/Primary?width=200&height=200&mode=Crop&quality=90&api_key={key}"
        if tag: 
            url += f"&tag={tag}"
            
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            return Response(
                content=resp.content, 
                media_type=resp.headers.get("Content-Type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"}
            )
    except: 
        pass
        
    return Response(status_code=404)