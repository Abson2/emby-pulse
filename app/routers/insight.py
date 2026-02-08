from fastapi import APIRouter, Request
from app.core.config import cfg
import requests
import time
import logging
import math

# 配置日志
logger = logging.getLogger("uvicorn")

router = APIRouter()

# 🔥 核心配置：每页只查 200 条，防止 Emby 内存溢出
BATCH_SIZE = 200

def get_emby_auth():
    """获取 Emby 配置信息"""
    return cfg.get("emby_host"), cfg.get("emby_api_key")

def fetch_with_retry(url, headers, retries=3):
    """
    带重试机制的请求函数
    """
    for i in range(retries):
        try:
            # 60秒超时
            response = requests.get(url, headers=headers, timeout=60)
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 500:
                logger.warning(f"Emby 服务端报错 500 (尝试 {i+1}/{retries})")
            else:
                logger.warning(f"Emby API 返回错误: {response.status_code} (尝试 {i+1}/{retries})")
        except requests.exceptions.Timeout:
            logger.warning(f"连接 Emby 超时 (尝试 {i+1}/{retries})")
        except requests.exceptions.RequestException as e:
            logger.error(f"连接 Emby 网络错误: {e}")
        
        if i == retries - 1: break
        time.sleep(1)
    return None

@router.get("/api/insight/quality")
def scan_library_quality(request: Request):
    """
    质量盘点核心接口 - 分页版
    """
    # 1. 鉴权
    user = request.session.get("user")
    if not user:
        return {"status": "error", "message": "Unauthorized: 请先登录"}
    
    host, key = get_emby_auth()
    if not host or not key:
        return {"status": "error", "message": "Emby 未配置，请前往[系统设置]填写 API Key"}

    headers = {"X-Emby-Token": key, "Accept": "application/json"}

    # 2. 定义分页获取函数
    def fetch_all_items_paged(item_type):
        all_items = []
        
        # A. 先只查总数 (Limit=0)
        count_url = f"{host}/emby/Items?Recursive=true&IncludeItemTypes={item_type}&Limit=0"
        count_data = fetch_with_retry(count_url, headers)
        
        if not count_data:
            logger.error(f"无法获取 {item_type} 总数，跳过扫描")
            return []
            
        total_count = count_data.get("TotalRecordCount", 0)
        logger.info(f"[{item_type}] 发现总数: {total_count}，准备分批拉取...")
        
        if total_count == 0:
            return []

        # B. 循环分页拉取
        # 计算总页数
        total_pages = math.ceil(total_count / BATCH_SIZE)
        
        for page in range(total_pages):
            start_index = page * BATCH_SIZE
            # 构造分页请求
            query = (
                f"Recursive=true&IncludeItemTypes={item_type}"
                f"&Fields=MediaSources,Path"  # 只查必须字段
                f"&StartIndex={start_index}&Limit={BATCH_SIZE}" # 🔥 关键：分页参数
            )
            url = f"{host}/emby/Items?{query}"
            
            # 打印进度日志
            logger.info(f"正在扫描 {item_type}: 第 {page+1}/{total_pages} 页 (Index {start_index})")
            
            data = fetch_with_retry(url, headers)
            if data and "Items" in data:
                all_items.extend(data["Items"])
            else:
                logger.warning(f"第 {page+1} 页获取失败，跳过该页")
                
            # 每页拉取完稍微停顿 0.1s，给 Emby 喘息时间
            time.sleep(0.1)
            
        return all_items

    try:
        # 3. 分别拉取电影和剧集
        movies = fetch_all_items_paged("Movie")
        episodes = fetch_all_items_paged("Episode")
        
        # 合并结果
        items = movies + episodes
        
        if not items:
            return {"status": "error", "message": "未扫描到有效媒体数据，请检查 Emby 状态"}

        logger.info(f"扫描完成，共获取 {len(items)} 条数据，开始统计分析...")

        # 4. 初始化统计
        stats = {
            "total_count": len(items),
            "resolution": {"4k": 0, "1080p": 0, "720p": 0, "sd": 0},
            "video_codec": {"hevc": 0, "h264": 0, "av1": 0, "other": 0},
            "hdr_type": {"sdr": 0, "hdr10": 0, "dolby_vision": 0},
            "bad_quality_list": []
        }

        # 5. 遍历统计 (逻辑不变)
        for item in items:
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
                if len(stats["bad_quality_list"]) < 100:
                    stats["bad_quality_list"].append({
                        "Name": item.get("Name"),
                        "SeriesName": item.get("SeriesName", ""),
                        "Year": item.get("ProductionYear"),
                        "Resolution": f"{width}x{video_stream.get('Height')}",
                        "Path": item.get("Path", "未知路径")
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
            if "dolby" in display_title or "dv" in display_title or "dolby" in video_range:
                stats["hdr_type"]["dolby_vision"] += 1
            elif "hdr" in video_range or "hdr" in display_title or "pq" in video_range:
                stats["hdr_type"]["hdr10"] += 1
            else:
                stats["hdr_type"]["sdr"] += 1

        return {"status": "success", "data": stats}

    except Exception as e:
        logger.error(f"质量盘点严重错误: {str(e)}")
        return {"status": "error", "message": f"处理失败: {str(e)}"}