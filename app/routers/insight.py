from fastapi import APIRouter, Request
from app.core.config import cfg
import requests
import time
import logging

logger = logging.getLogger("uvicorn")
router = APIRouter()

def get_emby_auth():
    return cfg.get("emby_host"), cfg.get("emby_api_key")

def fetch_json(url, headers, timeout=10):
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code == 200:
            return response.json()
        return None
    except:
        return None

@router.get("/api/insight/quality")
def scan_library_quality(request: Request):
    # 1. 鉴权
    user = request.session.get("user")
    if not user: return {"status": "error", "message": "未登录"}
    host, key = get_emby_auth()
    if not host: return {"status": "error", "message": "未配置 Emby"}

    headers = {"X-Emby-Token": key, "Accept": "application/json"}

    # 2. 初始化统计
    stats = {
        "total_count": 0,
        "resolution": {"4k": 0, "1080p": 0, "720p": 0, "sd": 0},
        "video_codec": {"hevc": 0, "h264": 0, "av1": 0, "other": 0},
        "hdr_type": {"sdr": 0, "hdr10": 0, "dolby_vision": 0},
        "bad_quality_list": []
    }

    # 3. 第一步：获取全量 ID 列表 (仅 ID，极速，不崩)
    logger.info("Step 1: 获取媒体库索引...")
    # Fields=Id 确保不加载元数据，只加载目录
    id_url = f"{host}/emby/Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=Id"
    
    # 这里增加重试，确保这一步必须成功
    data = None
    for _ in range(3):
        data = fetch_json(id_url, headers, timeout=30)
        if data: break
        time.sleep(1)

    if not data or "Items" not in data:
        return {"status": "error", "message": "无法连接 Emby 获取索引，请检查 Emby 是否存活"}

    all_ids = [i["Id"] for i in data["Items"]]
    total_len = len(all_ids)
    logger.info(f"Step 2: 索引获取成功，共 {total_len} 个条目，开始详情扫描...")

    # 4. 第二步：分批获取详情 (带自动降级策略)
    # 默认一批 20 个，如果崩了就降级为 1 个
    BATCH_SIZE = 20
    processed = 0
    
    i = 0
    while i < total_len:
        # 取出一批 ID
        batch_ids = all_ids[i : i + BATCH_SIZE]
        ids_str = ",".join(batch_ids)
        
        # 构造详情查询 URL
        url = f"{host}/emby/Items?Ids={ids_str}&Fields=MediaSources,Path,MediaStreams"
        
        # 尝试批量请求
        batch_data = None
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code == 200:
                batch_data = resp.json()
            else:
                # 如果非200，说明这批里有坏数据，触发降级
                logger.warning(f"Batch Error {resp.status_code}: 触发逐个扫描模式...")
        except:
            logger.warning("Batch Timeout: 触发逐个扫描模式...")

        # === 数据处理分支 ===
        valid_items = []
        
        if batch_data and "Items" in batch_data:
            # Plan A: 批量成功，直接用
            valid_items = batch_data["Items"]
        else:
            # Plan B: 批量失败，降级为【逐个扫描】这 20 个
            # 虽然慢，但能保证跳过坏数据，不影响整体
            for single_id in batch_ids:
                single_url = f"{host}/emby/Items?Ids={single_id}&Fields=MediaSources,Path,MediaStreams"
                item_data = fetch_json(single_url, headers, timeout=5)
                if item_data and "Items" in item_data and len(item_data["Items"]) > 0:
                    valid_items.extend(item_data["Items"])
                else:
                    logger.error(f"跳过损坏条目 ID: {single_id}")

        # === 统计逻辑 (统一处理) ===
        for item in valid_items:
            try:
                # 必须有多层防护，防止 Emby 返回空对象
                ms = item.get("MediaSources")
                if not ms: continue
                source = ms[0]
                streams = source.get("MediaStreams")
                if not streams: continue
                # 找视频流
                v_stream = next((s for s in streams if s.get("Type") == "Video"), None)
                if not v_stream: continue

                stats["total_count"] += 1
                
                # 分辨率
                w = v_stream.get("Width", 0)
                if w >= 3800: stats["resolution"]["4k"] += 1
                elif w >= 1900: stats["resolution"]["1080p"] += 1
                elif w >= 1200: stats["resolution"]["720p"] += 1
                else: 
                    stats["resolution"]["sd"] += 1
                    if len(stats["bad_quality_list"]) < 50:
                        stats["bad_quality_list"].append({
                            "Name": item.get("Name"),
                            "SeriesName": item.get("SeriesName", ""),
                            "Year": item.get("ProductionYear"),
                            "Resolution": f"{w}x{v_stream.get('Height')}",
                            "Path": item.get("Path", "")
                        })

                # 编码
                codec = v_stream.get("Codec", "").lower()
                if "hevc" in codec or "h265" in codec: stats["video_codec"]["hevc"] += 1
                elif "h264" in codec or "avc" in codec: stats["video_codec"]["h264"] += 1
                elif "av1" in codec: stats["video_codec"]["av1"] += 1
                else: stats["video_codec"]["other"] += 1

                # HDR
                vr = v_stream.get("VideoRange", "").lower()
                dt = v_stream.get("DisplayTitle", "").lower()
                if "dolby" in dt or "dv" in dt: stats["hdr_type"]["dolby_vision"] += 1
                elif "hdr" in vr or "hdr" in dt: stats["hdr_type"]["hdr10"] += 1
                else: stats["hdr_type"]["sdr"] += 1
            except:
                continue

        # 推进进度
        processed += len(batch_ids)
        if processed % 100 == 0:
            logger.info(f"进度: {processed} / {total_len}")
        
        i += BATCH_SIZE
        # 稍微休息，防止 Emby 再次过载
        time.sleep(0.2)

    return {"status": "success", "data": stats}