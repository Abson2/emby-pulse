from fastapi import APIRouter, Request, BackgroundTasks, HTTPException
from app.services.bot_service import bot
from app.core.config import cfg
import json
import logging

logger = logging.getLogger("uvicorn")
router = APIRouter()

@router.post("/api/v1/webhook")
async def emby_webhook(request: Request, background_tasks: BackgroundTasks):
    # 1. 鉴权
    query_token = request.query_params.get("token")
    if query_token != cfg.get("webhook_token"):
        logger.warning(f"🚫 Webhook 鉴权失败: {query_token}")
        raise HTTPException(status_code=403, detail="Invalid Token")

    try:
        # 2. 解析数据
        data = None
        content_type = request.headers.get("content-type", "")
        
        try:
            if "application/json" in content_type:
                data = await request.json()
            elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
                form = await request.form()
                raw_data = form.get("data")
                if raw_data:
                    data = json.loads(raw_data)
        except Exception as parse_err:
            logger.error(f"❌ 数据解析失败: {parse_err}")
            return {"status": "error", "message": "Payload parse failed"}

        if not data:
            return {"status": "error", "message": "Empty payload"}

        # 3. 事件分发
        event_raw = data.get("Event", "")
        event = event_raw.lower().strip()
        
        if event:
            logger.info(f"🔔 Webhook 事件: {event_raw}")

        # [场景A] 入库 (传递原始 item 数据，用于兜底)
        if event in ["library.new", "item.added"]:
            item = data.get("Item", {})
            item_id = item.get("Id")
            item_type = item.get("Type")
            if item_id and item_type in ["Movie", "Episode", "Series"]:
                # 🔥 关键：传入 item 原始数据
                background_tasks.add_task(bot.push_new_media, item_id, item)

        # [场景B] 播放开始
        elif event == "playback.start":
            background_tasks.add_task(bot.push_playback_event, data, "start")

        # [场景C] 播放停止 (记录数据)
        elif event == "playback.stop":
            background_tasks.add_task(bot.push_playback_event, data, "stop")
            background_tasks.add_task(bot.save_playback_activity, data)

        return {"status": "success"}
    
    except Exception as e:
        logger.error(f"❌ Webhook Error: {e}")
        return {"status": "error", "message": str(e)}