from fastapi import APIRouter, Request, BackgroundTasks
from app.services.bot_service import bot
import json

router = APIRouter()

@router.post("/api/v1/webhook")
async def emby_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    统一处理 Emby Webhook 事件
    """
    try:
        # 兼容性处理
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
        else:
            form = await request.form()
            raw_data = form.get("data", "{}")
            data = json.loads(raw_data)

        # 获取事件类型 (转小写，这是修复的关键！)
        event_raw = data.get("Event", "")
        event = event_raw.lower().strip()
        
        # 调试日志
        if event:
            print(f"🔔 Webhook收到事件: {event_raw}")

        # 1. 新资源入库 (兼容 library.new 和 item.added)
        if event in ["library.new", "item.added"]:
            item = data.get("Item", {})
            item_id = item.get("Id")
            item_type = item.get("Type")
            
            # 过滤不需要的类型
            if item_id and item_type in ["Movie", "Episode"]:
                print(f"   -> 准备推送入库: {item.get('Name')}")
                background_tasks.add_task(bot.push_new_media, item_id)

        # 2. 播放开始
        elif event == "playback.start":
            print(f"   -> 准备推送播放开始")
            background_tasks.add_task(bot.push_playback_event, data, "start")

        # 3. 播放停止
        elif event == "playback.stop":
            print(f"   -> 准备推送播放停止")
            background_tasks.add_task(bot.push_playback_event, data, "stop")

        return {"status": "success"}
    
    except Exception as e:
        print(f"❌ Webhook Error: {e}")
        return {"status": "error", "message": str(e)}