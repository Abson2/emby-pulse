from fastapi import APIRouter, Request, BackgroundTasks, HTTPException
from app.services.bot_service import bot
from app.core.config import cfg
import json
import logging

logger = logging.getLogger("uvicorn")
router = APIRouter()

@router.post("/api/v1/webhook")
async def emby_webhook(request: Request, background_tasks: BackgroundTasks):
    query_token = request.query_params.get("token")
    if query_token != cfg.get("webhook_token"):
        raise HTTPException(status_code=403, detail="Invalid Token")

    try:
        data = None
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await request.json()
        elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            raw_data = form.get("data")
            if raw_data: data = json.loads(raw_data)

        if not data: return {"status": "error", "message": "Empty"}

        event = data.get("Event", "").lower().strip()
        if event: logger.info(f"🔔 Webhook: {event}")

        # 🔥 核心修改：入库通知不再直接推送，而是丢入缓冲队列进行聚合
        if event in ["library.new", "item.added"]:
            item = data.get("Item", {})
            if item.get("Id") and item.get("Type") in ["Movie", "Episode", "Series"]:
                # 这一步非常快，不会阻塞 Webhook
                bot.add_library_task(item)

                # 🔥 新增：日历 Webhook 联动 (点亮绿灯)
                if item.get("Type") == "Episode":
                    series_id = item.get("SeriesId")
                    season = item.get("ParentIndexNumber")
                    episode = item.get("IndexNumber")
                    
                    if series_id and season is not None and episode is not None:
                        from app.services.calendar_service import calendar_service
                        calendar_service.mark_episode_ready(series_id, season, episode)

        # 2. 播放状态 (保持不变)
        elif event == "playback.start":
            background_tasks.add_task(bot.push_playback_event, data, "start")
        elif event == "playback.stop":
            background_tasks.add_task(bot.push_playback_event, data, "stop")

        return {"status": "success"}
    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return {"status": "error", "message": str(e)}