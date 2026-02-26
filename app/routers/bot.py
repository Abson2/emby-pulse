from fastapi import APIRouter, Request
from app.schemas.models import BotSettingsModel
from app.core.config import cfg
from app.services.bot_service import bot
import requests
import threading

router = APIRouter()

@router.get("/api/bot/settings")
def api_get_bot_settings(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    return {"status": "success", "data": cfg.get_all()}

@router.post("/api/bot/settings")
def api_save_bot_settings(data: BotSettingsModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    cfg.set("tg_bot_token", data.tg_bot_token); cfg.set("tg_chat_id", data.tg_chat_id)
    cfg.set("enable_bot", data.enable_bot)
    cfg.set("enable_notify", data.enable_notify)
    cfg.set("enable_library_notify", data.enable_library_notify) 
    
    bot.stop()
    if data.enable_bot: threading.Timer(1.0, bot.start).start()
    return {"status": "success", "message": "配置已保存"}

@router.post("/api/bot/test")
def api_test_bot(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    token = cfg.get("tg_bot_token"); chat_id = cfg.get("tg_chat_id"); proxy = cfg.get("proxy_url")
    if not token: return {"status": "error", "message": "请先保存配置"}
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        res = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": "🎉 测试消息"}, proxies=proxies, timeout=10)
        return {"status": "success"} if res.status_code == 200 else {"status": "error", "message": f"API Error: {res.text}"}
    except Exception as e: return {"status": "error", "message": str(e)}

# 🔥 新增辅助函数
def get_playback_url(item_id):
    # 优先用公网地址，没有则回退到内网 HOST
    base_url = cfg.get("emby_public_url") or cfg.get("emby_host")
    if base_url.endswith('/'): base_url = base_url[:-1]
    return f"{base_url}/web/index.html#!/item?id={item_id}"

# Webhook 接收 (如果使用了 Webhook 模式)
@router.post("/api/bot/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != cfg.get("tg_bot_token"):
        return {"status": "error", "message": "Invalid Token"}
        
    data = await request.json()
    
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"]
        
        if text.startswith("/search"):
            keyword = text.replace("/search", "").strip()
            if not keyword:
                send_tg_msg(chat_id, "🔍 请输入关键词，例如: /search 你的名字")
            else:
                items = search_emby(keyword)
                if not items:
                    send_tg_msg(chat_id, "TxT 未找到相关资源")
                else:
                    msg = f"🔍 搜索结果: {keyword}\n\n"
                    for item in items[:5]:
                        # 🔥 使用新的链接生成逻辑
                        link = get_playback_url(item['Id'])
                        msg += f"🎬 <b>{item['Name']}</b> ({item.get('ProductionYear', 'N/A')})\n"
                        msg += f"🔗 <a href='{link}'>点击播放</a>\n\n"
                    send_tg_msg(chat_id, msg)
                    
        elif text == "/start":
            send_tg_msg(chat_id, "👋 欢迎使用 EmbyPulse 机器人！\n支持指令:\n/search <关键词> - 搜索资源")
            
    return {"status": "success"}

def search_emby(keyword):
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        url = f"{host}/emby/Items?api_key={key}&Recursive=true&SearchTerm={keyword}&IncludeItemTypes=Movie,Series&Limit=5"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            return res.json().get("Items", [])
    except: pass
    return []

def send_tg_msg(chat_id, text):
    token = cfg.get("tg_bot_token"); proxy = cfg.get("proxy_url")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }, proxies=proxies, timeout=10)
    except: pass