import threading
import time
import requests
import datetime
import io
from app.core.config import cfg, REPORT_COVER_URL
from app.core.database import query_db, get_base_filter
from app.services.report_service import report_gen, HAS_PIL

class TelegramBot:
    def __init__(self):
        self.running = False
        self.poll_thread = None
        self.schedule_thread = None 
        self.offset = 0
        self.last_check_min = -1
        
    def start(self):
        if self.running: return
        if not cfg.get("tg_bot_token"): return
        self.running = True
        self._set_commands()
        self.poll_thread = threading.Thread(target=self._polling_loop, daemon=True)
        self.poll_thread.start()
        self.schedule_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.schedule_thread.start()
        print("🤖 Bot Started")

    def stop(self): self.running = False

    def _get_proxies(self):
        proxy = cfg.get("proxy_url")
        return {"http": proxy, "https": proxy} if proxy else None

    def _get_location(self, ip):
        if not ip or ip in ['127.0.0.1', '::1', '0.0.0.0']: return "本地连接"
        try:
            res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=3)
            if res.status_code == 200:
                d = res.json()
                if d.get('status') == 'success':
                    return f"{d.get('country')} {d.get('regionName')} {d.get('city')}"
        except: pass
        return "未知位置"

    def _download_emby_image(self, item_id, img_type='Primary'):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return None
        try:
            url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=1200&quality=90&api_key={key}"
            res = requests.get(url, timeout=15)
            if res.status_code == 200: return io.BytesIO(res.content)
        except: pass
        return None

    def send_photo(self, chat_id, photo_io, caption):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
            if isinstance(photo_io, str):
                data['photo'] = photo_io
                requests.post(url, data=data, proxies=self._get_proxies(), timeout=20)
            else:
                photo_io.seek(0)
                files = {"photo": ("image.jpg", photo_io, "image/jpeg")}
                requests.post(url, data=data, files=files, proxies=self._get_proxies(), timeout=30)
        except: self.send_message(chat_id, caption)

    def send_message(self, chat_id, text):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, proxies=self._get_proxies(), timeout=10)
        except: pass

    # ================= 核心推送逻辑 =================

    def push_playback_event(self, data, action="start"):
        if not cfg.get("enable_notify") or not cfg.get("tg_chat_id"): return
        try:
            cid = str(cfg.get("tg_chat_id"))
            user = data.get("User", {}).get("Name", "未知用户")
            item = data.get("Item", {})
            session = data.get("Session", {})
            
            title = item.get("Name", "未知内容")
            if item.get("SeriesName"):
                title = f"{item.get('SeriesName')} S{str(item.get('ParentIndexNumber',1)).zfill(2)}E{str(item.get('IndexNumber',0)).zfill(2)} {title}"

            ticks = data.get("PlaybackPositionTicks") or session.get("PlayState", {}).get("PositionTicks", 0)
            total = item.get("RunTimeTicks", 1)
            progress = f"{(ticks / total * 100):.2f}%" if total > 0 else "0.00%"
            ip = session.get("RemoteEndPoint", "127.0.0.1")
            loc = self._get_location(ip)

            emoji = "▶️" if action == "start" else "⏹️"
            act_txt = "开始播放" if action == "start" else "停止播放"
            msg = (
                f"{emoji} <b>【{user}】{act_txt}</b> {title}\n"
                f"📚 类型：{'剧集' if item.get('Type')=='Episode' else '电影'}\n"
                f"🔄 进度：{progress}\n"
                f"🌐 IP地址：{ip} {loc}\n"
                f"📱 设备：{session.get('Client','Emby')} {session.get('DeviceName','')}\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            img = self._download_emby_image(item.get("Id"), 'Backdrop') or self._download_emby_image(item.get("Id"), 'Primary')
            if img: self.send_photo(cid, img, msg)
            else: self.send_message(cid, msg)
        except: pass

    def push_new_media(self, item_id):
        if not cfg.get("enable_library_notify") or not cfg.get("tg_chat_id"): return
        cid = str(cfg.get("tg_chat_id")); host = cfg.get("emby_host"); key = cfg.get("emby_api_key")
        time.sleep(8) 
        try:
            res = requests.get(f"{host}/emby/Items/{item_id}?api_key={key}", timeout=10)
            if res.status_code != 200: return
            item = res.json()
            name = item.get("Name", "")
            if item.get("Type") == "Episode":
                name = f"{item.get('SeriesName','')} S{str(item.get('ParentIndexNumber',1)).zfill(2)}E{str(item.get('IndexNumber',1)).zfill(2)}"
            
            overview = item.get("Overview", "暂无简介...")
            if len(overview) > 120: overview = overview[:115] + "..."
            
            caption = (
                f"📺 <b>新入库 {name}</b>\n"
                f"⭐ 评分：{item.get('CommunityRating','N/A')}/10\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"📝 剧情：{overview}"
            )
            img = self._download_emby_image(item_id, 'Primary')
            # 降级策略：没海报也发，确保通知不丢失
            self.send_photo(cid, img if img else REPORT_COVER_URL, caption)
        except: pass

    # ================= 机器人指令实现 =================

    def _set_commands(self):
        token = cfg.get("tg_bot_token")
        cmds = [{"command": "stats", "description": "📊 超级日报"}, {"command": "now", "description": "🟢 正在播放"},
                {"command": "latest", "description": "🆕 最近入库"}, {"command": "recent", "description": "📜 播放历史"},
                {"command": "check", "description": "📡 系统检查"}, {"command": "help", "description": "🤖 帮助菜单"}]
        try: requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", json={"commands": cmds}, proxies=self._get_proxies())
        except: pass

    def _polling_loop(self):
        token = cfg.get("tg_bot_token"); admin_id = str(cfg.get("tg_chat_id"))
        while self.running:
            try:
                res = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"offset": self.offset, "timeout": 30}, proxies=self._get_proxies(), timeout=35)
                if res.status_code == 200:
                    for u in res.json().get("result", []):
                        self.offset = u["update_id"] + 1
                        if "message" in u:
                            cid = str(u["message"]["chat"]["id"])
                            if admin_id and cid != admin_id: continue
                            self._handle_message(u["message"], cid)
                else: time.sleep(5)
            except: time.sleep(5)

    def _handle_message(self, msg, cid):
        text = msg.get("text", "").strip()
        if text == "/stats": self._cmd_stats(cid)
        elif text == "/now": self._cmd_now(cid)
        elif text == "/latest": self._cmd_latest(cid)
        elif text == "/recent": self._cmd_recent(cid)
        elif text == "/check": self._cmd_check(cid)
        elif text == "/help": self._cmd_help(cid)

    def _cmd_stats(self, cid):
        where, params = get_base_filter('all')
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day')", params)[0]['c']
        users = query_db(f"SELECT user_name, COUNT(*) as cnt FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day') AND user_name != '' GROUP BY user_name ORDER BY cnt DESC LIMIT 5", params)
        user_txt = "\n".join([f"🏆 {u['user_name']} ({u['cnt']}次)" for u in users]) if users else "无"
        caption = f"📊 <b>今日媒体数据汇总</b>\n\n▶️ 今日播放：{plays} 次\n👥 活跃排行：\n{user_txt}"
        img = report_gen.generate_report('all', 'day') if HAS_PIL else REPORT_COVER_URL
        self.send_photo(cid, img, caption)

    def _cmd_now(self, cid):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=5)
            sessions = [s for s in res.json() if s.get("NowPlayingItem")]
            if not sessions: return self.send_message(cid, "🟢 当前暂无播放")
            msg = f"🟢 <b>正在播放 ({len(sessions)})</b>\n"
            for s in sessions:
                title = s['NowPlayingItem'].get('Name')
                user = s.get('UserName')
                progress = int(s.get('PlayState', {}).get('PositionTicks', 0) / s['NowPlayingItem'].get('RunTimeTicks', 1) * 100)
                msg += f"\n👤 <b>{user}</b>\n📺 {title}\n🔄 进度: {progress}%\n"
            self.send_message(cid, msg)
        except: self.send_message(cid, "❌ 无法获取状态")

    def _cmd_latest(self, cid):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            url = f"{host}/emby/Items?SortBy=DateCreated&SortOrder=Descending&IncludeItemTypes=Movie,Episode&Limit=5&Recursive=true&api_key={key}"
            items = requests.get(url, timeout=10).json().get("Items", [])
            msg = "🆕 <b>最近入库 Top 5</b>\n"
            for i in items:
                name = i.get("Name")
                if i.get("SeriesName"): name = f"{i.get('SeriesName')} - {name}"
                date = i.get("DateCreated", "")[:10]
                msg += f"\n📅 {date} | {name}"
            self.send_message(cid, msg)
        except: self.send_message(cid, "❌ 查询失败")

    def _cmd_recent(self, cid):
        rows = query_db("SELECT user_name, item_name, date_created FROM PlaybackActivity ORDER BY date_created DESC LIMIT 10")
        if not rows: return self.send_message(cid, "📭 无播放历史")
        msg = "📜 <b>最近 10 条历史</b>\n"
        for r in rows:
            date = r['date_created'].split('T')[0][5:]
            time = r['date_created'].split('T')[1][:5]
            msg += f"\n⏰ {date} {time} | {r['user_name']}\n🎬 {r['item_name']}\n"
        self.send_message(cid, msg)

    def _cmd_check(self, cid):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        start = time.time()
        try:
            res = requests.get(f"{host}/emby/System/Info?api_key={key}", timeout=5)
            if res.status_code == 200:
                info = res.json()
                local = (info.get('LocalAddresses') or [info.get('LocalAddress')])[0]
                wan = (info.get('RemoteAddresses') or [info.get('WanAddress')])[0]
                self.send_message(cid, f"✅ <b>系统在线</b>\n延迟: {int((time.time()-start)*1000)}ms\n内网: {local}\n外网: {wan}")
        except: self.send_message(cid, "❌ 连接错误")

    def _cmd_help(self, cid):
        msg = "🤖 <b>指令列表</b>\n/stats - 日报\n/now - 正在播放\n/latest - 最近入库\n/recent - 历史记录\n/check - 健康检查"
        self.send_message(cid, msg)

    def _scheduler_loop(self):
        while self.running:
            try:
                now = datetime.datetime.now()
                if now.minute != self.last_check_min:
                    self.last_check_min = now.minute
                    if now.hour == 9 and now.minute == 0:
                        self._check_user_expiration()
                        if cfg.get("tg_chat_id"): self._cmd_stats(str(cfg.get("tg_chat_id")))
                time.sleep(5)
            except: time.sleep(60)

    def _check_user_expiration(self):
        users = query_db("SELECT user_id, expire_date FROM users_meta WHERE expire_date IS NOT NULL AND expire_date != ''")
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        for u in users:
            if u['expire_date'] < today:
                try: requests.post(f"{host}/emby/Users/{u['user_id']}/Policy?api_key={key}", json={"IsDisabled": True})
                except: pass

    def push_now(self, user_id, period, theme):
        if not cfg.get("tg_chat_id"): return False
        img = report_gen.generate_report(user_id, period, theme) if HAS_PIL else REPORT_COVER_URL
        self.send_photo(str(cfg.get("tg_chat_id")), img, f"🚀 <b>立即推送</b>")
        return True

bot = TelegramBot()