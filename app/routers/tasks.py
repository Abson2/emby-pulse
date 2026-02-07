from fastapi import APIRouter, Request
from app.core.config import cfg
import requests

router = APIRouter()

def get_emby_auth():
    return cfg.get("emby_host"), cfg.get("emby_api_key")

@router.get("/api/tasks")
def get_scheduled_tasks(request: Request):
    """获取所有计划任务列表"""
    if not request.session.get("user"): return {"status": "error", "message": "Unauthorized"}
    
    host, key = get_emby_auth()
    if not host or not key: return {"status": "error", "message": "Emby 未配置"}

    try:
        url = f"{host}/emby/ScheduledTasks?api_key={key}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            tasks = res.json()
            # 按名称排序，把正在运行的排前面
            tasks.sort(key=lambda x: (x.get('State') != 'Running', x.get('Name')))
            return {"status": "success", "data": tasks}
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
        res = requests.post(url, timeout=5) # 注意：停止任务也是 POST 接口，但带 /Delete
        if res.status_code == 204:
            return {"status": "success", "message": "停止指令已发送"}
        return {"status": "error", "message": f"停止失败: {res.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}