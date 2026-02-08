from fastapi import APIRouter, Request
from app.core.config import cfg
import requests
import time

router = APIRouter()

def get_emby_auth():
    return cfg.get("emby_host"), cfg.get("emby_api_key")

def fetch_with_retry(url, headers, retries=3):
    """带重试机制的请求函数"""
    for i in range(retries):
        try:
            # 🔥 重点：将超时时间延长到 60 秒
            response = requests.get(url, headers=headers, timeout=60)
            if response.status_code == 200:
                return response.json()
        except requests.exceptions.RequestException:
            if i == retries - 1: raise
            time.sleep(1)
    return None

@router.get("/api/insight/scan")
def scan_library_quality(request: Request):
    """
    质量盘点核心逻辑
    """
    if not request.session.get("user"): 
        return {"status": "error", "message": "Unauthorized"}
    
    host, key = get_emby_auth()
    if not host or not key: 
        return {"status": "error", "message": "Emby 未配置，请先去系统设置填写 API Key"}

    try:
        headers = {"X-Emby-Token": key}
        
        # 1. 获取所有电影和剧集 (增加 Fields 参数确保获取详细元数据)
        # Emby 4.10 可能需要显式指定 Fields 才能获取 MediaSources
        query = "Recursive=true&IncludeItemTypes=Movie,Episode&Fields=MediaSources,ProviderIds,Path"
        url = f"{host}/emby/Items?{query}"
        
        data = fetch_with_retry(url, headers)
        items = data.get("Items", [])
        
        stats = {
            "total_count": len(items),
            "resolution": {"4k": 0, "1080p": 0, "720p": 0, "sd": 0},
            "video_codec": {"hevc": 0, "h264": 0, "av1": 0, "other": 0},
            "hdr_type": {"sdr": 0, "hdr10": 0, "dolby_vision": 0},
            "bad_quality_list": [] # 低画质洗版建议
        }

        for item in items:
            # 兼容性处理：防止某些条目没有 MediaSources
            if not item.get("MediaSources"): continue
            
            source = item["MediaSources"][0]
            if not source.get("MediaStreams"): continue
            
            video_stream = next((s for s in source["MediaStreams"] if s.get("Type") == "Video"), None)
            if not video_stream: continue

            # --- 分辨率统计 ---
            width = video_stream.get("Width", 0)
            if width >= 3800: stats["resolution"]["4k"] += 1
            elif width >= 1900: stats["resolution"]["1080p"] += 1
            elif width >= 1200: stats["resolution"]["720p"] += 1
            else: 
                stats["resolution"]["sd"] += 1
                # 记录低画质用于洗版建议 (仅记录前 50 个)
                if len(stats["bad_quality_list"]) < 50:
                    stats["bad_quality_list"].append({
                        "Name": item.get("Name"),
                        "SeriesName": item.get("SeriesName", ""),
                        "Year": item.get("ProductionYear"),
                        "Resolution": f"{width}x{video_stream.get('Height')}",
                        "Path": item.get("Path")
                    })

            # --- 编码统计 ---
            codec = video_stream.get("Codec", "").lower()
            if "hevc" in codec or "h265" in codec: stats["video_codec"]["hevc"] += 1
            elif "h264" in codec or "avc" in codec: stats["video_codec"]["h264"] += 1
            elif "av1" in codec: stats["video_codec"]["av1"] += 1
            else: stats["video_codec"]["other"] += 1

            # --- HDR 统计 ---
            # Emby 4.10 可能改变了 VideoRange 的返回方式，增加容错
            video_range = video_stream.get("VideoRange", "").lower()
            display_title = video_stream.get("DisplayTitle", "").lower()
            
            if "dolby" in display_title or "dv" in display_title:
                stats["hdr_type"]["dolby_vision"] += 1
            elif "hdr" in video_range or "hdr" in display_title:
                stats["hdr_type"]["hdr10"] += 1
            else:
                stats["hdr_type"]["sdr"] += 1

        return {"status": "success", "data": stats}

    except requests.exceptions.Timeout:
        return {"status": "error", "message": "连接 Emby 超时 (60s)，请检查 Emby 是否正在高负载运行"}
    except requests.exceptions.ConnectionError:
        return {"status": "error", "message": "连接 Emby 失败，请检查 IP/端口是否正确"}
    except Exception as e:
        return {"status": "error", "message": f"扫描失败: {str(e)}"}
EOF

echo "✅ 修复完成！请重启容器生效: docker-compose restart"