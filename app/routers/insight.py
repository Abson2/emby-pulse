from fastapi import APIRouter, Request
from app.core.config import cfg
import requests
import logging
import time
from datetime import datetime

# 配置日志
logger = logging.getLogger("uvicorn")

router = APIRouter()

# --- 🔥 全局缓存变量 ---
# 结构: {"data": dict, "timestamp": float}
GLOBAL_CACHE = {
    "quality_stats": None,
    "last_scan_time": 0
}
CACHE_EXPIRE_SECONDS = 86400  # 缓存有效期 24 小时

def get_emby_auth():
    """获取 Emby 配置信息"""
    return cfg.get("emby_host"), cfg.get("emby_api_key")

@router.get("/api/insight/quality")
def scan_library_quality(request: Request):
    """
    质量盘点 - 支持缓存与强制刷新
    参数: ?force_refresh=true
    """
    # 1. 鉴权检查
    user = request.session.get("user")
    if not user:
        return {"status": "error", "message": "Unauthorized: 请先登录"}
    
    # 2. 检查缓存 (如果不是强制刷新，且缓存未过期)
    force_refresh = request.query_params.get("force_refresh") == "true"
    current_time = time.time()
    
    if not force_refresh and GLOBAL_CACHE["quality_stats"] and (current_time - GLOBAL_CACHE["last_scan_time"] < CACHE_EXPIRE_SECONDS):
        logger.info("⚡ 使用质量盘点缓存数据")
        return {"status": "success", "data": GLOBAL_CACHE["quality_stats"]}

    # 3. 获取配置
    host, key = get_emby_auth()
    if not host or not key:
        return {"status": "error", "message": "Emby 未配置，请前往[系统设置]填写 API Key"}

    try:
        logger.info("🔄 开始执行 Emby 媒体库深度扫描...")
        
        # 4. 构造请求头
        headers = {
            "X-Emby-Token": key,
            "Accept": "application/json"
        }
        
        # 5. 构造标准查询 URL
        # 🔥 修改点：IncludeItemTypes 仅保留 Movie，剔除剧集干扰
        query_params = "Recursive=true&IncludeItemTypes=Movie&Fields=MediaSources,Path,MediaStreams,ProviderIds"
        url = f"{host}/emby/Items?{query_params}"
        
        # 6. 发起请求 (数据量大，给 60秒超时)
        response = requests.get(url, headers=headers, timeout=60)
        
        if response.status_code != 200:
            return {"status": "error", "message": f"Emby API Error: {response.status_code}"}
            
        data = response.json()
        items = data.get("Items", [])
        
        # 7. 初始化统计数据结构
        stats = {
            "total_count": len(items),
            "scan_time_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), # 记录扫描时间
            "resolution": {
                "4k": 0,      # 宽度 >= 3800
                "1080p": 0,   # 宽度 >= 1900
                "720p": 0,    # 宽度 >= 1200
                "sd": 0       # 其他
            },
            "video_codec": {
                "hevc": 0,    # H.265 / HEVC
                "h264": 0,    # H.264 / AVC
                "av1": 0,     # AV1
                "other": 0
            },
            "hdr_type": {
                "sdr": 0,
                "hdr10": 0,
                "dolby_vision": 0
            },
            "bad_quality_list": [] 
        }

        # 8. 遍历数据进行统计
        for item in items:
            # 安全检查：确保 item 包含 MediaSources
            media_sources = item.get("MediaSources")
            if not media_sources or not isinstance(media_sources, list):
                continue
            
            source = media_sources[0]
            media_streams = source.get("MediaStreams")
            if not media_streams:
                continue
            
            # 找到视频流 (Type=Video)
            video_stream = next((s for s in media_streams if s.get("Type") == "Video"), None)
            if not video_stream:
                continue

            # --- A. 分辨率统计 ---
            width = video_stream.get("Width", 0)
            if width >= 3800:
                stats["resolution"]["4k"] += 1
            elif width >= 1900:
                stats["resolution"]["1080p"] += 1
            elif width >= 1200:
                stats["resolution"]["720p"] += 1
            else: 
                stats["resolution"]["sd"] += 1
                # 记录低画质 (SD/480P) 用于前端展示洗版建议
                if len(stats["bad_quality_list"]) < 100:
                    stats["bad_quality_list"].append({
                        "Name": item.get("Name"),
                        "SeriesName": item.get("SeriesName", ""),
                        "Year": item.get("ProductionYear"),
                        "Resolution": f"{width}x{video_stream.get('Height')}",
                        "Path": item.get("Path", "未知路径")
                    })

            # --- B. 编码格式统计 ---
            codec = video_stream.get("Codec", "").lower()
            if "hevc" in codec or "h265" in codec:
                stats["video_codec"]["hevc"] += 1
            elif "h264" in codec or "avc" in codec:
                stats["video_codec"]["h264"] += 1
            elif "av1" in codec:
                stats["video_codec"]["av1"] += 1
            else:
                stats["video_codec"]["other"] += 1

            # --- C. HDR/杜比视界统计 ---
            video_range = video_stream.get("VideoRange", "").lower()
            display_title = video_stream.get("DisplayTitle", "").lower()
            
            if "dolby" in display_title or "dv" in display_title or "dolby" in video_range:
                stats["hdr_type"]["dolby_vision"] += 1
            elif "hdr" in video_range or "hdr" in display_title or "pq" in video_range:
                stats["hdr_type"]["hdr10"] += 1
            else:
                stats["hdr_type"]["sdr"] += 1

        # 🔥 更新缓存
        GLOBAL_CACHE["quality_stats"] = stats
        GLOBAL_CACHE["last_scan_time"] = current_time
        
        return {"status": "success", "data": stats}

    except Exception as e:
        logger.error(f"质量盘点错误: {str(e)}")
        return {"status": "error", "message": f"扫描失败: {str(e)}"}