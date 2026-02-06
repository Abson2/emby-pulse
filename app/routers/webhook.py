from fastapi import APIRouter, Request, BackgroundTasks
from app.services.bot_service import bot
import json

router = APIRouter()

@router.post("/api/v1/webhook")
async def emby_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
        else:
            form = await request.form()
            data = json.loads(form.get("data", "{}"))

        event_raw = data.get("Event", "")
        event = event_raw.lower().strip()
        
        # 调试日志：查看事件类型
        if event: print(f"🔔 Webhook Event: {event_raw}")

        # 1. 媒体库变动 (新入库)
        if event in ["library.new", "item.added"]:
            item = data.get("Item", {})
            item_id = item.get("Id")
            item_type = item.get("Type")
            
            # 支持 Movie, Episode 以及 Series(剧集本身)
            if item_id and item_type in ["Movie", "Episode", "Series"]:
                background_tasks.add_task(bot.push_new_media, item_id)

        # 2. 播放开始
        elif event == "playback.start":
            background_tasks.add_task(bot.push_playback_event, data, "start")

        # 3. 播放停止
        elif event == "playback.stop":
            background_tasks.add_task(bot.push_playback_event, data, "stop")

        return {"status": "success"}
    except Exception as e:
        print(f"❌ Webhook Error: {e}")
        return {"status": "error", "message": str(e)}