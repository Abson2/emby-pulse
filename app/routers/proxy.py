from fastapi import APIRouter, Response
from app.core.config import cfg
import requests
import logging
from functools import lru_cache

# 设置日志，方便在 Docker 控制台看到报错
logger = logging.getLogger("uvicorn")

router = APIRouter()

# 🔥 核心魔法：智能 ID 转换缓存
# 使用 lru_cache 缓存查询结果，避免重复请求 Emby API 导致页面卡顿
@lru_cache(maxsize=4096)
def get_real_image_id(item_id: str):
    """
    智能判断：如果是单集 (Episode)，尝试向上寻找剧集 ID (SeriesId)
    从而获取竖屏海报，而不是横屏剧照
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    # 基础检查
    if not key or not host: 
        print(f"⚠️ [Proxy] Missing Config: key={bool(key)}, host={bool(host)}")
        return item_id

    try:
        # 查询 Item 详情
        # ⚠️ 注意：超时时间延长到 5 秒，防止 NAS 响应慢
        url = f"{host}/emby/Items/{item_id}?api_key={key}"
        res = requests.get(url, timeout=5) 
        
        if res.status_code == 200:
            data = res.json()
            type_raw = data.get("Type", "")
            series_id = data.get("SeriesId")
            
            # 调试日志 (第一次访问某个 ID 时会打印)
            # print(f"🔍 [Proxy] Checking {item_id}: Type={type_raw}, SeriesId={series_id}")

            # 如果是单集(Episode) 或 季(Season)，优先返回 SeriesId
            if type_raw in ["Episode", "Season"] and series_id:
                return series_id
            
            # 如果是剧集(Series)或电影(Movie)，直接返回原 ID
            return item_id
        else:
            print(f"❌ [Proxy] Emby API Error: {res.status_code} for {item_id}")
            
    except Exception as e:
        # 查询失败时(如网络超时)，打印具体的错误原因！
        print(f"❌ [Proxy] Smart Resolve Failed for {item_id}: {str(e)}")
        pass
    
    # 默认返回原 ID
    return item_id

@router.get("/api/proxy/image/{item_id}/{img_type}")
def proxy_image(item_id: str, img_type: str):
    """
    代理 Emby 图片资源 (智能版)
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    if not key or not host:
        return Response(status_code=404)

    try:
        # 🔥 关键修改：只有请求 Primary (封面) 时才进行智能替换
        # Backdrop (背景图) 依然保持单集原图，这样详情页背景更准确
        target_id = item_id
        if img_type.lower() == 'primary':
            target_id = get_real_image_id(item_id)

        # 构造 Emby 图片 URL
        # 增加 quality=90 和尺寸限制，优化加载速度
        url = f"{host}/emby/Items/{target_id}/Images/{img_type}?maxHeight=600&maxWidth=400&quality=90&api_key={key}"
        
        # 发起请求
        resp = requests.get(url, timeout=10, stream=True)
        
        if resp.status_code == 200:
            # 透传图片
            return Response(
                content=resp.content, 
                media_type=resp.headers.get("Content-Type", "image/jpeg"),
                # 🔥 调试模式：暂时禁用缓存 (no-cache)
                # 这样您刷新网页时，一定会强制向服务器请求新图，解决浏览器缓存问题
                # 等一切正常后，您可以改回 "public, max-age=86400"
                headers={"Cache-Control": "no-cache"} 
            )
    except Exception as e:
        print(f"❌ [Proxy] Image Download Error: {e}")
        pass
        
    # 失败则返回 404，前端会显示默认图
    return Response(status_code=404)

@router.get("/api/proxy/user_image/{user_id}")
def proxy_user_image(user_id: str, tag: str = None):
    """
    代理用户头像 (保持不变)
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    if not key: return Response(status_code=404)
        
    try:
        url = f"{host}/emby/Users/{user_id}/Images/Primary?width=200&height=200&mode=Crop&quality=90&api_key={key}"
        if tag: url += f"&tag={tag}"
            
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            return Response(
                content=resp.content, 
                media_type=resp.headers.get("Content-Type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"}
            )
    except: pass
        
    return Response(status_code=404)