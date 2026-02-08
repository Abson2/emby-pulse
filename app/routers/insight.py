from fastapi import APIRouter, Request
from app.core.config import cfg
import sqlite3
import os
import logging
import shutil
import tempfile

# 配置日志
logger = logging.getLogger("uvicorn")

router = APIRouter()

def find_library_db():
    """
    寻找 Emby 的核心数据库 library.db
    通常在 /emby-data/data/library.db 或 /emby-data/library.db
    """
    # 容器内的挂载点，根据 docker-compose.yml 应该是 /emby-data
    base_paths = [
        "/emby-data/data/library.db",
        "/emby-data/library.db",
        "/config/data/library.db", # 兼容某些通过 config 挂载的情况
        "/config/library.db"
    ]
    
    for path in base_paths:
        if os.path.exists(path):
            logger.info(f"✅ 发现数据库文件: {path}")
            return path
            
    # 如果找不到，尝试搜索
    if os.path.exists("/emby-data"):
        for root, dirs, files in os.walk("/emby-data"):
            if "library.db" in files:
                path = os.path.join(root, "library.db")
                logger.info(f"✅ 搜索到数据库文件: {path}")
                return path
    
    return None

def get_stats_from_db(db_path):
    """
    直接查询 SQLite 数据库，绕过 API
    """
    stats = {
        "total_count": 0,
        "resolution": {"4k": 0, "1080p": 0, "720p": 0, "sd": 0},
        "video_codec": {"hevc": 0, "h264": 0, "av1": 0, "other": 0},
        "hdr_type": {"sdr": 0, "hdr10": 0, "dolby_vision": 0},
        "bad_quality_list": []
    }

    # 为了防止锁库，先复制一份到临时目录读取，读完即删
    temp_dir = tempfile.gettempdir()
    temp_db = os.path.join(temp_dir, "emby_pulse_library_temp.db")
    
    try:
        shutil.copy2(db_path, temp_db)
    except Exception as e:
        logger.error(f"复制数据库失败: {e}")
        return None, f"无法读取数据库文件: {str(e)}"

    conn = None
    try:
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
        
        # SQL 查询：关联 MediaItems 和 MediaStreams
        # Type 'Movie' 和 'Episode' 在 DB 中通常直接存储为字符串
        sql = """
        SELECT 
            I.Name, 
            I.SeriesName, 
            I.ProductionYear, 
            I.Path,
            S.Width, 
            S.Height, 
            S.Codec, 
            S.VideoRange, 
            S.DisplayTitle
        FROM MediaItems I
        JOIN MediaStreams S ON I.Id = S.ItemId
        WHERE I.Type IN ('Movie', 'Episode') 
          AND S.StreamType = 'Video'
        """
        
        cursor.execute(sql)
        rows = cursor.fetchall()
        
        stats["total_count"] = len(rows)
        logger.info(f"数据库查询成功，获取到 {len(rows)} 条视频流数据")

        for row in rows:
            name, series_name, year, path, width, height, codec, video_range, display_title = row
            
            # 数据清洗（防止 None）
            width = width if width else 0
            height = height if height else 0
            codec = codec.lower() if codec else ""
            video_range = video_range.lower() if video_range else ""
            display_title = display_title.lower() if display_title else ""
            path = path if path else ""

            # 1. 分辨率统计
            if width >= 3800: stats["resolution"]["4k"] += 1
            elif width >= 1900: stats["resolution"]["1080p"] += 1
            elif width >= 1200: stats["resolution"]["720p"] += 1
            else: 
                stats["resolution"]["sd"] += 1
                if len(stats["bad_quality_list"]) < 100:
                    stats["bad_quality_list"].append({
                        "Name": name,
                        "SeriesName": series_name,
                        "Year": year,
                        "Resolution": f"{width}x{height}",
                        "Path": path
                    })

            # 2. 编码统计
            if "hevc" in codec or "h265" in codec: stats["video_codec"]["hevc"] += 1
            elif "h264" in codec or "avc" in codec: stats["video_codec"]["h264"] += 1
            elif "av1" in codec: stats["video_codec"]["av1"] += 1
            else: stats["video_codec"]["other"] += 1

            # 3. HDR 统计
            if "dolby" in display_title or "dv" in display_title or "dolby" in video_range:
                stats["hdr_type"]["dolby_vision"] += 1
            elif "hdr" in video_range or "hdr" in display_title or "pq" in video_range:
                stats["hdr_type"]["hdr10"] += 1
            else:
                stats["hdr_type"]["sdr"] += 1
        
        return stats, None

    except Exception as e:
        logger.error(f"SQL 查询错误: {e}")
        return None, f"数据库结构不兼容: {str(e)}"
    finally:
        if conn: conn.close()
        # 清理临时文件
        if os.path.exists(temp_db):
            os.remove(temp_db)


@router.get("/api/insight/quality")
def scan_library_quality(request: Request):
    """
    质量盘点 - 数据库直连版
    """
    # 1. 鉴权
    user = request.session.get("user")
    if not user: return {"status": "error", "message": "Unauthorized"}
    
    # 2. 寻找数据库
    db_path = find_library_db()
    if not db_path:
        return {
            "status": "error", 
            "message": "未找到 library.db 文件。请检查 docker-compose.yml 是否正确映射了 /emby-data 目录。"
        }

    # 3. 执行查询
    data, err = get_stats_from_db(db_path)
    
    if err:
        return {"status": "error", "message": err}
    
    return {"status": "success", "data": data}