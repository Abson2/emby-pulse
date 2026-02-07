from fastapi import APIRouter
from app.core.config import cfg
import requests

router = APIRouter()

@router.get("/api/insight/quality")
def get_quality_stats():
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    if not key or not host:
        return {"status": "error", "msg": "未配置 Emby"}

    try:
        # 查询所有电影，包含媒体流信息
        # Limit=5000 防止库太大超时，如有需要可加大
        fields = "MediaSources,MediaStreams,ProductionYear"
        url = f"{host}/emby/Items?IncludeItemTypes=Movie&Recursive=true&Fields={fields}&Limit=5000&api_key={key}"
        
        res = requests.get(url, timeout=30) # 扫描可能较慢，给30秒
        if res.status_code != 200:
            return {"status": "error", "msg": "连接 Emby 失败"}
            
        items = res.json().get("Items", [])
        
        # 初始化统计容器
        stats = {
            "total": len(items),
            "resolution": {"4K": 0, "1080P": 0, "720P": 0, "SD": 0},
            "hdr": {"SDR": 0, "HDR10": 0, "Dolby Vision": 0},
            "codec": {"HEVC (H.265)": 0, "AVC (H.264)": 0, "Other": 0},
            "low_quality_list": [] # 低画质名单
        }

        for item in items:
            # 提取视频流
            sources = item.get("MediaSources", [])
            if not sources: continue
            
            # 通常取第一个源的第一个视频流
            video = next((s for s in sources[0].get("MediaStreams", []) if s.get("Type") == "Video"), None)
            if not video: continue

            # 1. 分辨率统计
            w = video.get("Width", 0)
            if w >= 3800: 
                stats["resolution"]["4K"] += 1
            elif w >= 1900: 
                stats["resolution"]["1080P"] += 1
            elif w >= 1200: 
                stats["resolution"]["720P"] += 1
            else: 
                stats["resolution"]["SD"] += 1
                # 将 SD (低于720P) 加入清洗名单
                stats["low_quality_list"].append({
                    "Id": item.get("Id"),
                    "Name": item.get("Name"),
                    "Year": item.get("ProductionYear"),
                    "Res": f"{w}x{video.get('Height')}"
                })

            # 2. HDR 统计
            v_range = video.get("VideoRange", "").upper()
            title = video.get("DisplayTitle", "").upper()
            
            if "DOVI" in title or "DOLBY VISION" in title:
                stats["hdr"]["Dolby Vision"] += 1
            elif "HDR" in v_range or "HDR" in title:
                stats["hdr"]["HDR10"] += 1
            else:
                stats["hdr"]["SDR"] += 1

            # 3. 编码统计
            codec = video.get("Codec", "").lower()
            if "hevc" in codec or "h265" in codec:
                stats["codec"]["HEVC (H.265)"] += 1
            elif "avc" in codec or "h264" in codec:
                stats["codec"]["AVC (H.264)"] += 1
            else:
                stats["codec"]["Other"] += 1

        # 清洗名单只取前 20 个，避免页面过长
        stats["low_quality_list"] = stats["low_quality_list"][:20]

        return {"status": "success", "data": stats}

    except Exception as e:
        return {"status": "error", "msg": str(e)}