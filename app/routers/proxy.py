from fastapi import APIRouter, Response
from app.core.config import cfg
import requests
import logging

# 初始化日志
logger = logging.getLogger("uvicorn")
router = APIRouter()

def get_real_image_id_robust(item_id: str):
    """
    智能 ID 转换（暴力增强版）
    尝试多种姿势向 Emby 获取 SeriesId
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    if not key or not host: return item_id

    # 定义通用请求头
    params_base = {"api_key": key}

    # -------------------------------------------------------
    # 方案 A: 标准查询 (查询单集详情)
    # -------------------------------------------------------
    try:
        url_a = f"{host}/emby/Items/{item_id}"
        # 强制请求 SeriesId, ParentId
        res_a = requests.get(url_a, params={**params_base, "Fields": "SeriesId,ParentId"}, timeout=3)
        
        if res_a.status_code == 200:
            data = res_a.json()
            if data.get("SeriesId"):
                print(f"✅ [Plan A] Found SeriesId: {data['SeriesId']} via Detail")
                return data['SeriesId']
            if data.get("Type") == "Episode" and data.get("ParentId"):
                print(f"🔄 [Plan A] Using ParentId: {data['ParentId']}")
                return data['ParentId']
    except: pass

    # -------------------------------------------------------
    # 方案 B: 祖先查询 (查询父级链) -> 专门解决权限/层级问题
    # -------------------------------------------------------
    try:
        url_b = f"{host}/emby/Items/{item_id}/Ancestors"
        res_b = requests.get(url_b, params=params_base, timeout=3)
        
        if res_b.status_code == 200:
            ancestors = res_b.json()
            # 祖先列表通常是从近到远 [Season, Series, ...]
            for ancestor in ancestors:
                if ancestor.get("Type") == "Series":
                    print(f"✅ [Plan B] Found SeriesId: {ancestor['Id']} via Ancestors")
                    return ancestor['Id']
                if ancestor.get("Type") == "Season" and not ancestor.get("SeriesId"):
                    # 如果只有季ID，先拿着
                    return ancestor['Id']
    except: pass

    # -------------------------------------------------------
    # 方案 C: 列表查询 (有时列表接口比详情接口权限宽)
    # -------------------------------------------------------
    try:
        url_c = f"{host}/emby/Items"
        # 查这个ID，并且递归
        res_c = requests.get(url_c, params={**params_base, "Ids": item_id, "Fields": "SeriesId", "Recursive": "true"}, timeout=3)
        
        if res_c.status_code == 200:
            items = res_c.json().get("Items", [])
            if items and items[0].get("SeriesId"):
                print(f"✅ [Plan C] Found SeriesId: {items[0]['SeriesId']} via List")
                return items[0]['SeriesId']
    except: pass

    # 3次尝试都失败，确实没办法了，打印红色警告提示用户检查权限
    print(f"❌ [Failed] Could not resolve SeriesId for {item_id}. (Check API Key Permissions!)")
    return item_id

@router.get("/api/proxy/image/{item_id}/{img_type}")
def proxy_image(item_id: str, img_type: str):
    """
    图片代理路由
    """
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if not key or not host: return Response(status_code=404)

    try:
        target_id = item_id
        
        # 仅对 Primary (封面) 启用增强查询
        if img_type.lower() == 'primary':
            target_id = get_real_image_id_robust(item_id)

        # 构造 URL
        url = f"{host}/emby/Items/{target_id}/Images/{img_type}?maxHeight=600&maxWidth=400&quality=90&api_key={key}"
        
        resp = requests.get(url, timeout=10, stream=True)
        
        if resp.status_code == 200:
            return Response(
                content=resp.content, 
                media_type=resp.headers.get("Content-Type", "image/jpeg"),
                headers={"Cache-Control": "no-cache"} # 调试期间禁用缓存
            )
        
        # 兜底：如果转换后的 ID 失败，回退原 ID
        if resp.status_code == 404 and target_id != item_id:
            fallback_url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=600&maxWidth=400&quality=90&api_key={key}"
            fallback_resp = requests.get(fallback_url, timeout=10, stream=True)
            if fallback_resp.status_code == 200:
                 return Response(
                    content=fallback_resp.content, 
                    media_type=fallback_resp.headers.get("Content-Type", "image/jpeg"),
                    headers={"Cache-Control": "no-cache"}
                )

    except Exception: pass
    return Response(status_code=404)

@router.get("/api/proxy/user_image/{user_id}")
def proxy_user_image(user_id: str, tag: str = None):
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if not key: return Response(status_code=404)
    try:
        url = f"{host}/emby/Users/{user_id}/Images/Primary?width=200&height=200&mode=Crop&quality=90&api_key={key}"
        if tag: url += f"&tag={tag}"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
    except: pass
    return Response(status_code=404)