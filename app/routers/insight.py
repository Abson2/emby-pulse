from fastapi import APIRouter, Request
from app.core.config import cfg
import requests
import time
import logging

# 配置日志
logger = logging.getLogger("uvicorn")

router = APIRouter()

def get_emby_auth():
    return cfg.get("emby_host"), cfg.get("emby_api_key")

def fetch_with_retry(url, headers, retries=2):
    """基础请求函数"""
    for i in range(retries):
        try:
            # 缩短超时时间，避免单个卡死影响整体
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                return None # 正常忽略
        except Exception:
            pass
        time.sleep(0.5)
    return None

@router.get("/api/insight/quality")
def scan_library_quality(request: Request):
    """
    质量盘点 - 按媒体库隔离扫描策略
    """
    # 1. 鉴权
    user = request.session.get("user")
    if not user: return {"status": "error", "message": "Unauthorized"}
    
    host, key = get_emby_auth()
    if not host or not key: return {"status": "error", "message": "Emby 未配置"}

    headers = {"X-Emby-Token": key, "Accept": "application/json"}
    
    # 2. 核心逻辑：先获取所有“媒体库”(Views)
    # 这样我们可以一个库一个库的扫，避开全局查询的 NullReferenceException
    views_url = f"{host}/emby/Library/SelectableMediaFolders"
    views_data = fetch_with_retry(views_url, headers)
    
    if not views_data:
        # 如果获取不到库，尝试回退到用户视图
        views_url = f"{host}/emby/Users/{user['id']}/Views"
        views_data = fetch_with_retry(views_url, headers)

    if not views_data or "Items" not in views_data:
         return {"status": "error", "message": "无法获取媒体库列表，Emby API 异常"}

    # 3. 初始化统计
    stats = {
        "total_count": 0,
        "resolution": {"4k": 0, "1080p": 0, "720p": 0, "sd": 0},
        "video_codec": {"hevc": 0, "h264": 0, "av1": 0, "other": 0},
        "hdr_type": {"sdr": 0, "hdr10": 0, "dolby_vision": 0},
        "bad_quality_list": []
    }

    scanned_items_count = 0
    
    # 4. 遍历每个媒体库进行扫描
    for folder in views_data["Items"]:
        folder_id = folder.get("Id")
        folder_name = folder.get("Name")
        collection_type = folder.get("CollectionType", "unknown")
        
        # 只扫描电影和剧集类型的库
        if collection_type not in ["movies", "tvshows"]:
            continue
            
        logger.info(f"正在扫描媒体库: [{folder_name}] (ID: {folder_id})...")
        
        # 构造查询：指定 ParentId，限制在当前库内
        # 移除了 Limit=0 的总数检查，直接分页查数据，减少交互次数
        # 降低 BATCH_SIZE 到 100，进一步降低崩溃概率
        BATCH_SIZE = 100
        start_index = 0
        
        while True:
            # 构造安全查询
            # 关键：指定 ParentId = folder_id，不再进行全局递归
            query = (
                f"ParentId={folder_id}&Recursive=true"
                f"&IncludeItemTypes=Movie,Episode"
                f"&Fields=MediaSources,Path,MediaStreams" # 必须字段
                f"&StartIndex={start_index}&Limit={BATCH_SIZE}"
            )
            url = f"{host}/emby/Items?{query}"
            
            data = fetch_with_retry(url, headers)
            
            if not data or "Items" not in data or len(data["Items"]) == 0:
                break # 当前库扫完了
                
            items = data["Items"]
            scanned_items_count += len(items)
            
            # --- 处理数据统计 (和之前逻辑一致) ---
            for item in items:
                # 容错处理
                media_sources = item.get("MediaSources")
                if not media_sources or not isinstance(media_sources, list): continue
                source = media_sources[0]
                media_streams = source.get("MediaStreams")
                if not media_streams: continue
                video_stream = next((s for s in media_streams if s.get("Type") == "Video"), None)
                if not video_stream: continue

                # 分辨率
                width = video_stream.get("Width", 0)
                if width >= 3800: stats["resolution"]["4k"] += 1
                elif width >= 1900: stats["resolution"]["1080p"] += 1
                elif width >= 1200: stats["resolution"]["720p"] += 1
                else: 
                    stats["resolution"]["sd"] += 1
                    if len(stats["bad_quality_list"]) < 50:
                        stats["bad_quality_list"].append({
                            "Name": item.get("Name"),
                            "SeriesName": item.get("SeriesName", ""),
                            "Year": item.get("ProductionYear"),
                            "Resolution": f"{width}x{video_stream.get('Height')}",
                            "Path": item.get("Path", "")
                        })

                # 编码
                codec = video_stream.get("Codec", "").lower()
                if "hevc" in codec or "h265" in codec: stats["video_codec"]["hevc"] += 1
                elif "h264" in codec or "avc" in codec: stats["video_codec"]["h264"] += 1
                elif "av1" in codec: stats["video_codec"]["av1"] += 1
                else: stats["video_codec"]["other"] += 1

                # HDR
                video_range = video_stream.get("VideoRange", "").lower()
                display_title = video_stream.get("DisplayTitle", "").lower()
                if "dolby" in display_title or "dv" in display_title: stats["hdr_type"]["dolby_vision"] += 1
                elif "hdr" in video_range or "hdr" in display_title: stats["hdr_type"]["hdr10"] += 1
                else: stats["hdr_type"]["sdr"] += 1

            # 翻页
            start_index += BATCH_SIZE
            # 安全熔断：单个库最多扫 20000 条，防止死循环
            if start_index > 20000: break
            time.sleep(0.1)

    stats["total_count"] = scanned_items_count
    return {"status": "success", "data": stats}