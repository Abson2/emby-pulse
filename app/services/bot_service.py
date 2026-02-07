import threading
import time
import requests
import datetime
import io
import json
import logging
from app.core.config import cfg, REPORT_COVER_URL, FALLBACK_IMAGE_URL
from app.core.database import query_db, get_base_filter
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
        
        print("🤖 Bot Service Started (Ultimate Edition)")

    def stop(self): 
        self.running = False

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
        """
        下载图片流
        支持通过 image_tag 直接拼接 URL，绕过 API 查询
        """
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return None
        try:
            # 如果提供了 image_tag，直接拼接 URL (最快，无视 404)
            if image_tag:
                url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=1200&quality=90&tag={image_tag}"
            else:
                # 否则走 API 鉴权流程
                url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=1200&quality=90&api_key={key}"
            
            res = requests.get(url, timeout=15)
            if res.status_code == 200: return io.BytesIO(res.content)
        except Exception as e:
            logger.error(f"Image Download Failed: {e}")
        return None

    def send_photo(self, chat_id, photo_io, caption, parse_mode="HTML"):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode}
            
            if isinstance(photo_io, str): 
                data['photo'] = photo_io
                requests.post(url, data=data, proxies=self._get_proxies(), timeout=20)
            else:
                photo_io.seek(0)
                files = {"photo": ("image.jpg", photo_io, "image/jpeg")}
                requests.post(url, data=data, files=files, proxies=self._get_proxies(), timeout=30)
        except Exception as e: 
            logger.error(f"Send Photo Error: {e}")
            self.send_message(chat_id, caption)

    def send_message(self, chat_id, text, parse_mode="HTML"):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}, proxies=self._get_proxies(), timeout=10)
        except Exception as e: 
            logger.error(f"Send Message Error: {e}")

    # ================= 业务逻辑 =================

    def save_playback_activity(self, data):
        """记录播放数据 (含时长)"""
        try:
            user = data.get("User", {})
            item = data.get("Item", {})
            session = data.get("Session", {})
            
            user_id = user.get("Id")
            user_name = user.get("Name")
            item_id = item.get("Id")
            item_name = item.get("Name")
            item_type = item.get("Type")
            device_name = session.get("DeviceName")
            client = session.get("Client")
            
            # 🔥 记录播放时长 (RunTimeTicks)
            # 注意：这里记录的是媒体的总时长，作为这次观看的"权重"
            # 如果需要精确记录看了多少秒，需要用 PlaybackPositionTicks 计算差值，比较复杂
            # 现阶段为了恢复旧日报逻辑，通常记录 Item 的 RunTimeTicks 或者 0
            playback_ticks = item.get("RunTimeTicks", 0)
            
            date_created = datetime.datetime.now().isoformat()
            
            if user_id and item_id:
                sql = """
                INSERT INTO PlaybackActivity 
                (user_id, user_name, item_id, item_name, item_type, device_name, client, date_created, playback_ticks) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                query_db(sql, (user_id, user_name, item_id, item_name, item_type, device_name, client, date_created, playback_ticks))
                logger.info(f"✅ 播放记录入库: {user_name} - {item_name}")
        except Exception as e:
            logger.error(f"❌ 数据库写入失败: {e}")

    def push_playback_event(self, data, action="start"):
        if not cfg.get("enable_notify") or not cfg.get("tg_chat_id"): return
        try:
            chat_id = str(cfg.get("tg_chat_id"))
            user = data.get("User", {})
            item = data.get("Item", {})
            session = data.get("Session", {})
            
            title = item.get("Name", "未知内容")
            if item.get("SeriesName"): 
                idx = item.get("IndexNumber", 0)
                parent_idx = item.get("ParentIndexNumber", 1)
                title = f"{item.get('SeriesName')} S{str(parent_idx).zfill(2)}E{str(idx).zfill(2)} {title}"

            type_cn = "剧集" if item.get("Type") == "Episode" else "电影"
            
            # 进度双重检查
            ticks = data.get("PlaybackPositionTicks")
            if ticks is None: ticks = session.get("PlayState", {}).get("PositionTicks", 0)
            total = item.get("RunTimeTicks", 1)
            pct = f"{(ticks / total * 100):.2f}%" if total > 0 else "0.00%"

            emoji = "▶️" if action == "start" else "⏹️"
            act_txt = "开始播放" if action == "start" else "停止播放"
            ip = session.get("RemoteEndPoint", "127.0.0.1")
            loc = self._get_location(ip)

            msg = (
                f"{emoji} <b>【{user.get('Name')}】{act_txt}</b>\n"
                f"📺 {title}\n"
                f"📚 类型：{type_cn}\n"
                f"🔄 进度：{pct}\n"
                f"🌐 地址：{ip} ({loc})\n"
                f"📱 设备：{session.get('Client')} on {session.get('DeviceName')}\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            # 尝试发送图片
            item_id = item.get("Id")
            img_io = self._download_emby_image(item_id, 'Backdrop') or self._download_emby_image(item_id, 'Primary')
            if img_io: self.send_photo(chat_id, img_io, msg)
            else: self.send_message(chat_id, msg)
            
        except Exception as e: logger.error(f"Push Playback Error: {e}")

    def push_new_media(self, item_id, fallback_item=None):
        """
        处理入库通知 (API 404 免疫版)
        """
        if not cfg.get("enable_library_notify") or not cfg.get("tg_chat_id"): return
        
        chat_id = str(cfg.get("tg_chat_id"))
        host = cfg.get("emby_host"); key = cfg.get("emby_api_key")

        # 1. 优先尝试从 Webhook 原始数据中获取 ImageTag
        # 这是解决 404 问题的终极方案：不需要问 API，直接算图的 URL
        direct_image_tag = None
        if fallback_item:
            direct_image_tag = fallback_item.get("ImageTags", {}).get("Primary")

        # 2. 如果原始数据里有图，直接发，零等待！
        if direct_image_tag:
            logger.info(f"🚀 检测到 Webhook 包含 ImageTag，跳过 API 查询，直接发送！")
            item = fallback_item
        else:
            # 3. 只有原始数据没图时，才去轮询 API (保留之前的逻辑作为备选)
            item = None
            for i in range(3):
                wait_time = 10 + (i * 15)
                logger.info(f"⏳ Webhook 无图，等待 API 扫描 ({wait_time}s)... [第{i+1}次]")
                time.sleep(wait_time)
                try:
                    res = requests.get(f"{host}/emby/Items/{item_id}?api_key={key}", timeout=10)
                    if res.status_code == 200:
                        item = res.json()
                        if item.get("ImageTags", {}).get("Primary"): break
                except: pass
        
        # 4. 最终兜底：API 也查不到，就用 fallback_item
        final_item = item if item else fallback_item
        if not final_item: return

        try:
            name = final_item.get("Name", "未知标题")
            type_raw = final_item.get("Type", "Movie")
            overview = final_item.get("Overview", "暂无简介...")
            rating = final_item.get("CommunityRating", "N/A")
            year = final_item.get("ProductionYear", "")
            
            if len(overview) > 150: overview = overview[:140] + "..."
            
            type_cn = "电影"
            display_title = name
            if type_raw == "Episode":
                type_cn = "剧集"
                s_name = final_item.get("SeriesName", "")
                s_idx = final_item.get("ParentIndexNumber", 1)
                e_idx = final_item.get("IndexNumber", 1)
                display_title = f"{s_name} S{str(s_idx).zfill(2)}E{str(e_idx).zfill(2)}"
                if name and "Episode" not in name: display_title += f" {name}"
            elif type_raw == "Series": type_cn = "剧集"

            caption = (
                f"📺 <b>新入库 {type_cn}</b>\n{display_title} ({year})\n\n"
                f"⭐ 评分：{rating}/10\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"📝 剧情：{overview}"
            )

            # 获取图片 (优先用 ImageTag)
            img_tag = final_item.get("ImageTags", {}).get("Primary")
            img_io = self._download_emby_image(item_id, 'Primary', image_tag=img_tag)
            
            if img_io:
                self.send_photo(chat_id, img_io, caption)
            else:
                logger.info("⚠️ 最终无图，使用默认封面")
                self.send_photo(chat_id, REPORT_COVER_URL, caption)

        except Exception as e: logger.error(f"Push New Media Error: {e}")

    # ================= 指令系统 =================

    def _set_commands(self):
        token = cfg.get("tg_bot_token")
        cmds = [
            {"command": "stats", "description": "📊 超级日报"},
            {"command": "now", "description": "🟢 正在播放"},
            {"command": "latest", "description": "🆕 最近入库"},
            {"command": "recent", "description": "📜 播放历史"},
            {"command": "check", "description": "📡 系统检查"},
            {"command": "help", "description": "🤖 帮助菜单"}
        ]
        try: requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", json={"commands": cmds}, proxies=self._get_proxies())
        except: pass

    def _polling_loop(self):
        token = cfg.get("tg_bot_token"); admin_id = str(cfg.get("tg_chat_id"))
        while self.running:
            try:
                url = f"https://api.telegram.org/bot{token}/getUpdates"
                res = requests.get(url, params={"offset": self.offset, "timeout": 30}, proxies=self._get_proxies(), timeout=35)
                if res.status_code == 200:
                    for u in res.json().get("result", []):
                        self.offset = u["update_id"] + 1
                        if "message" in u: 
                            chat_id = str(u["message"]["chat"]["id"])
                            if admin_id and chat_id != admin_id: continue 
                            self._handle_message(u["message"], chat_id)
                else: time.sleep(5)
            except: time.sleep(5)

    def _handle_message(self, msg, chat_id):
        text = msg.get("text", "").strip()
        if text == "/stats": self._cmd_stats(chat_id)
        elif text == "/now": self._cmd_now(chat_id)
        elif text == "/latest": self._cmd_latest(cid=chat_id)
        elif text == "/recent": self._cmd_recent(chat_id)
        elif text == "/check": self._cmd_check(chat_id)
        elif text == "/help": self._cmd_help(chat_id)

    # 🔥 修复版日报逻辑：恢复 Top 3 和 时长统计
    def _cmd_stats(self, chat_id):
        where, params = get_base_filter('all')
        
        # 1. 基础统计
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day')", params)[0]['c']
        
        # 2. 活跃时长 (Ticks -> Hours)
        # 1 Tick = 100ns, 1s = 10,000,000 Ticks
        duration_row = query_db(f"SELECT SUM(playback_ticks) as t FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day')", params)
        total_ticks = duration_row[0]['t'] if duration_row and duration_row[0]['t'] else 0
        total_hours = round(total_ticks / (10000 * 1000 * 3600), 1)

        # 3. 活跃用户数
        users_count = query_db(f"SELECT COUNT(DISTINCT user_name) as c FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day')", params)[0]['c']

        # 4. 榜首之星
        top_user = query_db(f"SELECT user_name, SUM(playback_ticks) as t FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day') GROUP BY user_name ORDER BY t DESC LIMIT 1", params)
        top_user_str = "暂无"
        if top_user:
            u_hours = round(top_user[0]['t'] / (10000 * 1000 * 3600), 1)
            top_user_str = f"{top_user[0]['user_name']} ({u_hours}h)"

        # 5. 热门内容 Top 3
        top_items = query_db(f"SELECT item_name, COUNT(*) as c FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day') GROUP BY item_name ORDER BY c DESC LIMIT 3", params)
        
        top_content = ""
        medals = ["🥇", "🥈", "🥉"]
        if top_items:
            for idx, item in enumerate(top_items):
                top_content += f"{medals[idx]} {item['item_name']} ({item['c']}次)\n"
        else:
            top_content = "暂无数据"

        today = datetime.datetime.now().strftime('%Y-%m-%d')
        
        # 组装旧版风格的文本
        caption = (
            f"📊 <b>EmbyPulse 今日日报</b>\n"
            f"📅 {today}\n"
            f"───────────────\n"
            f"📈 <b>数据大盘</b>\n"
            f"▶️ 总播放量: {plays} 次\n"
            f"⏱️ 活跃时长: {total_hours} 小时\n"
            f"👥 活跃人数: {users_count} 人\n"
            f"👑 榜首之星: {top_user_str}\n"
            f"───────────────\n"
            f"🔥 <b>热门内容 Top 3</b>\n"
            f"{top_content}"
        )

        if HAS_PIL:
            img = report_gen.generate_report('all', 'day')
            self.send_photo(chat_id, img, caption)
        else:
            self.send_photo(chat_id, REPORT_COVER_URL, caption)

    def _cmd_now(self, chat_id):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=5)
            sessions = [s for s in res.json() if s.get("NowPlayingItem")]
            if not sessions:
                self.send_message(chat_id, "🟢 当前没有正在播放的会话")
                return
            msg = f"🟢 <b>正在播放 ({len(sessions)})</b>\n"
            for s in sessions:
                user = s.get('UserName')
                title = s['NowPlayingItem'].get('Name')
                ticks = s.get('PlayState', {}).get('PositionTicks', 0)
                total = s['NowPlayingItem'].get('RunTimeTicks', 1)
                pct = int((ticks / total) * 100) if total > 0 else 0
                msg += f"\n👤 <b>{user}</b> | 🔄 {pct}%\n📺 {title}\n"
            self.send_message(chat_id, msg)
        except: self.send_message(chat_id, "❌ 无法连接 Emby 服务器")

    def _cmd_latest(self, cid): # 修复参数名
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            url = f"{host}/emby/Items?SortBy=DateCreated&SortOrder=Descending&IncludeItemTypes=Movie,Episode&Limit=5&Recursive=true&api_key={key}"
            res = requests.get(url, timeout=10)
            items = res.json().get("Items", [])
            msg = "🆕 <b>最近入库 Top 5</b>\n"
            for item in items:
                name = item.get("Name")
                if item.get("SeriesName"): name = f"{item.get('SeriesName')} - {name}"
                date = item.get("DateCreated", "")[:10]
                msg += f"\n📅 {date} | {name}"
            self.send_message(cid, msg)
        except: self.send_message(cid, "❌ 查询失败")

    def _cmd_recent(self, chat_id):
        try:
            rows = query_db("SELECT user_name, item_name, date_created FROM PlaybackActivity ORDER BY date_created DESC LIMIT 10")
            if not rows: return self.send_message(chat_id, "📭 暂无播放记录")
            msg = "📜 <b>最近 10 条播放记录</b>\n"
            for r in rows:
                date = r['date_created'].split('T')[0][5:]
                time_part = r['date_created'].split('T')[1][:5]
                msg += f"\n⏰ {date} {time_part} | {r['user_name']}\n🎬 {r['item_name']}\n"
            self.send_message(chat_id, msg)
        except: self.send_message(chat_id, "❌ 查询失败")

    def _cmd_check(self, chat_id):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        start = time.time()
        try:
            res = requests.get(f"{host}/emby/System/Info?api_key={key}", timeout=5)
            latency = int((time.time() - start) * 1000)
            if res.status_code == 200:
                info = res.json()
                local = (info.get('LocalAddresses') or [info.get('LocalAddress')])[0]
                wan = (info.get('RemoteAddresses') or [info.get('WanAddress')])[0]
                msg = f"✅ <b>系统连接正常</b>\n📡 延迟: {latency}ms\n🏠 内网: {local}\n🌍 外网: {wan}"
                self.send_message(chat_id, msg)
        except: self.send_message(chat_id, "❌ 连接错误")

    def _cmd_help(self, chat_id):
        msg = "🤖 <b>EmbyPulse 指令列表</b>\n/stats - 日报\n/now - 正在播放\n/latest - 最近入库\n/recent - 历史记录\n/check - 健康检查"
        self.send_message(chat_id, msg)

    def _scheduler_loop(self):
        while self.running:
            try:
                now = datetime.datetime.now()
                if now.minute != self.last_check_min:
                    self.last_check_min = now.minute
                    if now.hour == 9 and now.minute == 0:
                        self._check_user_expiration()
                        if cfg.get("tg_chat_id") and cfg.get("enable_bot"):
                            self._cmd_stats(str(cfg.get("tg_chat_id")))
                time.sleep(5)
            except: time.sleep(60)

    def _check_user_expiration(self):
        users = query_db("SELECT user_id, expire_date FROM users_meta WHERE expire_date IS NOT NULL AND expire_date != ''")
        if not users: return
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        for u in users:
            if u['expire_date'] < today:
                try: requests.post(f"{host}/emby/Users/{u['user_id']}/Policy?api_key={key}", json={"IsDisabled": True})
                except: pass
    
    def push_now(self, user_id, period, theme):
        if not cfg.get("tg_chat_id"): return False
        if HAS_PIL:
            img = report_gen.generate_report(user_id, period, theme)
            self.send_photo(str(cfg.get("tg_chat_id")), img, f"🚀 <b>立即推送</b>")
        else:
            self._cmd_stats(str(cfg.get("tg_chat_id")))
        return True

bot = TelegramBot()