import threading
import time
import requests
import datetime
import io
import logging
from app.core.config import cfg, REPORT_COVER_URL
from app.core.database import query_db
from app.services.report_service import report_gen, HAS_PIL

logger = logging.getLogger("uvicorn")

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
        
        print("🤖 Bot Service Started (Plugin Read Mode)")

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

    def _download_emby_image(self, item_id, img_type='Primary', image_tag=None):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return None
        try:
            if image_tag:
                url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=1200&quality=90&tag={image_tag}"
            else:
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

    # ================= 业务逻辑 =================

    # 🔥 1. 废弃写入：因为 Emby 插件已经写了，我们不需要重复写
    def save_playback_activity(self, data):
        pass 

    # 2. 播放通知
    def push_playback_event(self, data, action="start"):
        if not cfg.get("enable_notify") or not cfg.get("tg_chat_id"): return
        try:
            cid = str(cfg.get("tg_chat_id"))
            user = data.get("User", {})
            item = data.get("Item", {})
            session = data.get("Session", {})
            
            title = item.get("Name", "未知内容")
            if item.get("SeriesName"): 
                title = f"{item.get('SeriesName')} S{str(item.get('ParentIndexNumber',1)).zfill(2)}E{str(item.get('IndexNumber',0)).zfill(2)} {title}"

            ticks = data.get("PlaybackPositionTicks")
            if ticks is None: ticks = session.get("PlayState", {}).get("PositionTicks", 0)
            total = item.get("RunTimeTicks", 1)
            pct = f"{(ticks / total * 100):.2f}%" if total > 0 else "0.00%"

            emoji = "▶️" if action == "start" else "⏹️"
            act = "开始播放" if action == "start" else "停止播放"
            ip = session.get("RemoteEndPoint", "127.0.0.1")
            loc = self._get_location(ip)

            msg = (
                f"{emoji} <b>【{user.get('Name')}】{act}</b>\n"
                f"📺 {title}\n"
                f"🔄 进度：{pct}\n"
                f"🌐 地址：{ip} ({loc})\n"
                f"📱 设备：{session.get('Client')} on {session.get('DeviceName')}\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            img = self._download_emby_image(item.get("Id"), 'Backdrop') or self._download_emby_image(item.get("Id"), 'Primary')
            if img: self.send_photo(cid, img, msg)
            else: self.send_message(cid, msg)
        except: pass

    # 3. 入库通知 (带 ImageTag 优化)
    def push_new_media(self, item_id, fallback_item=None):
        if not cfg.get("enable_library_notify") or not cfg.get("tg_chat_id"): return
        cid = str(cfg.get("tg_chat_id")); host = cfg.get("emby_host"); key = cfg.get("emby_api_key")

        # 优先用 Webhook 里的图
        if fallback_item and fallback_item.get("ImageTags", {}).get("Primary"):
            item = fallback_item
        else:
            # 否则重试查 API
            item = None
            for i in range(3):
                time.sleep(10 + i*15)
                try:
                    res = requests.get(f"{host}/emby/Items/{item_id}?api_key={key}", timeout=10)
                    if res.status_code == 200:
                        item = res.json()
                        break
                except: pass
        
        final = item if item else fallback_item
        if not final: return

        try:
            name = final.get("Name", "")
            if final.get("Type") == "Episode":
                name = f"{final.get('SeriesName','')} S{str(final.get('ParentIndexNumber',1)).zfill(2)}E{str(final.get('IndexNumber',1)).zfill(2)}"
            
            caption = (
                f"📺 <b>新入库 {final.get('Type','影视')}</b>\n{name} ({final.get('ProductionYear','')})\n\n"
                f"⭐ 评分：{final.get('CommunityRating','N/A')}/10\n"
                f"📝 剧情：{final.get('Overview','暂无简介...')[:140]}..."
            )
            
            # 优先使用 ImageTag 拼接，无需 API 权限
            tag = final.get("ImageTags", {}).get("Primary")
            img = self._download_emby_image(item_id, 'Primary', image_tag=tag)
            self.send_photo(cid, img if img else REPORT_COVER_URL, caption)
        except: pass

    # ================= 指令系统 =================

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

    # 🔥 修复版日报：适配 Emby 插件数据库列名 (PascalCase)
    def _cmd_stats(self, cid):
        # 基础过滤：排除隐藏用户 (注意列名 UserId)
        where = "WHERE 1=1"
        hidden = cfg.get("hidden_users")
        if hidden: where += f" AND UserId NOT IN ({','.join(['?']*len(hidden))})"
        
        # 1. 播放量 (DateCreated 是字符串，比较当天)
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day')", hidden)[0]['c']
        
        # 2. 活跃时长 (PlayDuration 是秒)
        dur = query_db(f"SELECT SUM(PlayDuration) as t FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day')", hidden)
        total_h = round(dur[0]['t'] / 3600, 1) if dur and dur[0]['t'] else 0.0

        # 3. 活跃用户 (UserName)
        users = query_db(f"SELECT COUNT(DISTINCT UserName) as c FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day')", hidden)[0]['c']

        # 4. 榜首之星
        top = query_db(f"SELECT UserName, SUM(PlayDuration) as t FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day') GROUP BY UserName ORDER BY t DESC LIMIT 1", hidden)
        top_str = f"{top[0]['UserName']} ({round(top[0]['t']/3600, 1)}h)" if top else "暂无"

        # 5. 热门内容 (ItemName)
        tops = query_db(f"SELECT ItemName, COUNT(*) as c FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day') GROUP BY ItemName ORDER BY c DESC LIMIT 3", hidden)
        top_content = ""
        for i, item in enumerate(tops):
            top_content += f"{['🥇','🥈','🥉'][i]} {item['ItemName']} ({item['c']}次)\n"

        today = datetime.datetime.now().strftime('%Y-%m-%d')
        caption = (
            f"📊 <b>EmbyPulse 今日日报</b>\n📅 {today}\n───────────────\n"
            f"📈 <b>数据大盘</b>\n▶️ 总播放量: {plays} 次\n⏱️ 活跃时长: {total_h} 小时\n"
            f"👥 活跃人数: {users} 人\n👑 榜首之星: {top_str}\n"
            f"───────────────\n🔥 <b>热门内容 Top 3</b>\n{top_content or '暂无数据'}"
        )
        
        if HAS_PIL:
            # 注意：如果 report_service 也没适配列名，这里生成的图可能还是没数据
            # 暂时只发文字版或者确保 report_service 也改了
            self.send_photo(cid, REPORT_COVER_URL, caption)
        else:
            self.send_photo(cid, REPORT_COVER_URL, caption)

    def _cmd_now(self, cid):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=5)
            sessions = [s for s in res.json() if s.get("NowPlayingItem")]
            if not sessions: return self.send_message(cid, "🟢 当前无播放")
            msg = f"🟢 <b>正在播放 ({len(sessions)})</b>\n"
            for s in sessions:
                title = s['NowPlayingItem'].get('Name')
                pct = int(s.get('PlayState', {}).get('PositionTicks', 0) / s['NowPlayingItem'].get('RunTimeTicks', 1) * 100)
                msg += f"\n👤 <b>{s.get('UserName')}</b> | 🔄 {pct}%\n📺 {title}\n"
            self.send_message(cid, msg)
        except: self.send_message(cid, "❌ 连接失败")

    def _cmd_latest(self, cid):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            url = f"{host}/emby/Items?SortBy=DateCreated&SortOrder=Descending&IncludeItemTypes=Movie,Episode&Limit=5&Recursive=true&api_key={key}"
            items = requests.get(url, timeout=10).json().get("Items", [])
            msg = "🆕 <b>最近入库</b>\n"
            for i in items:
                name = i.get("Name")
                if i.get("SeriesName"): name = f"{i.get('SeriesName')} - {name}"
                msg += f"\n📅 {i.get('DateCreated', '')[:10]} | {name}"
            self.send_message(cid, msg)
        except: self.send_message(cid, "❌ 查询失败")

    def _cmd_recent(self, cid):
        # 适配插件列名：UserName, ItemName, DateCreated
        rows = query_db("SELECT UserName, ItemName, DateCreated FROM PlaybackActivity ORDER BY DateCreated DESC LIMIT 10")
        if not rows: return self.send_message(cid, "📭 无记录")
        msg = "📜 <b>最近播放</b>\n"
        for r in rows:
            date = r['DateCreated'][:16].replace('T', ' ')
            msg += f"\n⏰ {date} | {r['UserName']}\n🎬 {r['ItemName']}\n"
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
                self.send_message(cid, f"✅ <b>在线</b>\n延迟: {int((time.time()-start)*1000)}ms\n内网: {local}\n外网: {wan}")
        except: self.send_message(cid, "❌ 离线")

    def _cmd_help(self, cid):
        self.send_message(cid, "🤖 /stats, /now, /latest, /recent, /check")

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
        self._cmd_stats(str(cfg.get("tg_chat_id"))) # 暂时只推送文字版，待图片生成逻辑适配后再切回
        return True

bot = TelegramBot()