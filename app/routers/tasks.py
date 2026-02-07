from fastapi import APIRouter, Request
from app.core.config import cfg
import requests

router = APIRouter()

def get_emby_auth():
    return cfg.get("emby_host"), cfg.get("emby_api_key")

# 🔥 任务名称汉化字典
TRANS_MAP = {
    # 核心/系统
    "Scan Media Library": "扫描媒体库 (全量)",
    "Refresh People": "刷新人物信息",
    "Rotate Log File": "日志文件轮转",
    "Check for application updates": "检查主程序更新",
    "Check for plugin updates": "检查插件更新",
    "Cache file cleanup": "清理缓存文件",
    "Clean Transcode Directory": "清理转码目录",
    "Hardware Detection": "硬件转码检测",
    "Emby Server Backup": "服务器配置备份",
    
    # 媒体处理
    "Convert media": "媒体格式转换",
    "Create Playlists": "生成智能播放列表",
    "Extract Chapter Images": "提取章节预览图",
    "Chapter image extraction": "提取章节预览图",
    "Thumbnail image extraction": "提取视频缩略图",
    "Download subtitles": "自动下载字幕",
    "Organize new media files": "自动整理新文件",
    
    # 常见插件 - 豆瓣/刮削
    "Build Douban Cache": "构建豆瓣缓存",
    "Download OCR Data": "下载 OCR 识别数据",
    
    # 常见插件 - Intro Skip / 媒体分析
    "Detect Episode Intros": "检测剧集片头 (Intro)",
    "Extract Intro Fingerprint": "提取片头指纹",
    "Extract MediaInfo": "提取媒体编码信息",
    "Extract Video Thumbnail": "提取视频缩略图 (覆盖)",
    
    # 常见插件 - 维护/清理
    "Delete Persons": "清理无效人物数据",
    "Export Library to Trakt": "同步库到 Trakt",
    "Trakt Sync": "Trakt 同步"
}

# 🔥 类别汉化与排序权重
CAT_MAP = {
    "Library": {"name": "📚 媒体库", "order": 1},
    "System": {"name": "⚡ 系统核心", "order": 2},
    "Maintenance": {"name": "🧹 维护保养", "order": 3},
    "Application": {"name": "📱 应用程序", "order": 4},
    "Metadata": {"name": "📝 元数据", "order": 5},
    "Downloads": {"name": "📥 下载管理", "order": 6},
    "Sync": {"name": "🔄 同步与备份", "order": 7},
    "Live TV": {"name": "📺 电视直播", "order": 8}
}

@router.get("/api/tasks")
def get_scheduled_tasks(request: Request):
    """获取所有计划任务列表（已汉化+分组）"""
    if not request.session.get("user"): return {"status": "error", "message": "Unauthorized"}
    
    host, key = get_emby_auth()
    if not host or not key: return {"status": "error", "message": "Emby 未配置"}

    try:
        url = f"{host}/emby/ScheduledTasks?api_key={key}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            raw_tasks = res.json()
            
            # 分组容器
            grouped = {}
            
            for t in raw_tasks:
                # 1. 汉化名称
                t['OriginalName'] = t.get('Name')
                t['Name'] = TRANS_MAP.get(t['Name'], t['Name']) # 查不到字典就用原名
                
                # 2. 识别类别
                cat_raw = t.get('Category', 'Other')
                
                # 如果类别在字典里，用字典的；否则视为“插件”
                if cat_raw in CAT_MAP:
                    cat_display = CAT_MAP[cat_raw]["name"]
                    order = CAT_MAP[cat_raw]["order"]
                else:
                    # 比如 "神医助手", "Douban" 等
                    cat_display = f"🧩 插件: {cat_raw}"
                    order = 99 # 插件排在最后
                
                # 3. 归类
                if order not in grouped:
                    grouped[order] = {"title": cat_display, "tasks": []}
                grouped[order]["tasks"].append(t)
            
            # 4. 按顺序转为列表
            final_list = []
            for k in sorted(grouped.keys()):
                # 组内按名称排序
                grouped[k]["tasks"].sort(key=lambda x: x['Name'])
                final_list.append(grouped[k])
                
            return {"status": "success", "data": final_list}
            
        return {"status": "error", "message": f"Emby Error: {res.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/api/tasks/{task_id}/start")
def start_task(task_id: str, request: Request):
    """手动触发任务"""
    if not request.session.get("user"): return {"status": "error"}
    
    host, key = get_emby_auth()
    try:
        url = f"{host}/emby/ScheduledTasks/Running/{task_id}?api_key={key}"
        res = requests.post(url, timeout=5)
        if res.status_code == 204:
            return {"status": "success", "message": "任务已启动"}
        return {"status": "error", "message": f"启动失败: {res.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/api/tasks/{task_id}/stop")
def stop_task(task_id: str, request: Request):
    """停止正在运行的任务"""
    if not request.session.get("user"): return {"status": "error"}
    
    host, key = get_emby_auth()
    try:
        url = f"{host}/emby/ScheduledTasks/Running/{task_id}/Delete?api_key={key}"
        res = requests.post(url, timeout=5)
        if res.status_code == 204:
            return {"status": "success", "message": "停止指令已发送"}
        return {"status": "error", "message": f"停止失败: {res.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}