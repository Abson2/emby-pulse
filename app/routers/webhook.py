from fastapi import APIRouter, HTTPException, Request, BackgroundTasks
from app.services.bot_service import bot
from app.core.config import cfg
import json

router = APIRouter()

@router.post("/api/v1/webhook")
async def emby_webhook(request: Request, background_tasks: BackgroundTasks):
    # 验证 Webhook Token
    query_token = request.query_params.get("token")
    if query_token != cfg.get("webhook_token"):
        print(f"🚫 Webhook 授权失败: {query_token}")
        raise HTTPException(status_code=403, detail="Invalid Webhook Token")

    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
        else:
            form = await request.form()
            data = json.loads(form.get("data", "{}"))

        event_raw = data.get("Event", "").lower().strip()
        print(f"🔔 Webhook 收到事件: {event_raw}")

        # 处理入库通知
        if event_raw in ["library.new", "item.added"]:
            item = data.get("Item", {})
            if item.get("Id") and item.get("Type") in ["Movie", "Episode", "Series"]:
                # 这里我们直接推送到 Telegram
                background_tasks.add_task(bot.push_new_media, item.get("Id"))

        # 处理播放事件
        elif event_raw == "playback.start":
            background_tasks.add_task(bot.push_playback_event, data, "start")
        elif event_raw == "playback.stop":
            background_tasks.add_task(bot.push_playback_event, data, "stop")

        return {"status": "success"}
    except Exception as e:
        print(f"❌ Webhook 处理错误: {e}")
        return {"status": "error", "message": str(e)}