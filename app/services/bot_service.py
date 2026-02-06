import threading
import time
import requests
import datetime
import io
import re
from app.core.config import cfg, REPORT_COVER_URL, FALLBACK_IMAGE_URL
from app.core.database import query_db, get_base_filter
from app.services.report_service import report_gen, HAS_PIL
import logging

# 初始化 Logger
logger = logging.getLogger("uvicorn")

class TelegramBot:
    def __init__(self):
        self.running = False
        self.poll_thread = None
        self.monitor_thread = None  # 保留原始的监控线程逻辑
        self.schedule_thread = None 
        self.offset = 0
        self.active_sessions = {}
        self.last_check_min = -1
        
    def start(self):
        if self.running: return
        if not cfg.get("enable_bot") or not cfg.get("tg_bot_token"): return
        self.running = True
        self._set_commands()
        
        # 1. 消息监听线程
        self.poll_thread = threading.Thread(target=self._polling_loop, daemon=True)
        self.poll_thread.start()
        
        # 2. 定时任务线程 (早报 & 用户检查)
        self.schedule_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.schedule_thread.start()
        
        # 3. 原始轮询线程 (可选保留，但现在有了 Webhook，此线程可作为兜底)
        if cfg.get("enable_notify"):
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
            
        print("🤖 Bot Started (Full Business Logic Restored)")

    def stop(self): 
        self.running = False

    def _get_proxies(self):
        proxy = cfg.get("proxy_url")
        return {"http": proxy, "https": proxy} if proxy else None

    # ================= 工具函数 =================

    def _get_location(self, ip):
        """IP 归属地查询"""
        if not ip or ip in ['127.0.0.1', '::1', '0.0.0.0']: return "本地局域网"
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
            print(f"Bot Photo Error: {e}")
            self.send_message(chat_id, caption)

    def send_message(self, chat_id, text, parse_mode="HTML"):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}, proxies=self._get_proxies(), timeout=10)
        except Exception as e:
            print(f"Bot Send Error: {e}")

    # ================= Webhook 推送业务逻辑 =================

    def push_playback_event(self, data, action="start"):
        """按照要求美化的播放通知格式"""
        if not cfg.get("enable_notify") or not cfg.get("tg_chat_id"): return
        try:
            cid = str(cfg.get("tg_chat_id"))
            user = data.get("User", {}).get("Name", "未知用户")
            item = data.get("Item", {})
            session = data.get("Session", {})
            
            # 标题与剧集格式
            title = item.get("Name", "未知内容")
            series_name = item.get("SeriesName")
            if series_name:
                idx = item.get("IndexNumber", 0)
                p_idx = item.get("ParentIndexNumber", 1)
                title = f"剧集 {series_name} S{str(p_idx).zfill(2)}E{str(idx).zfill(2)} {title}"

            # 进度计算
            pos = data.get("PlaybackPositionTicks") or session.get("PlayState", {}).get("PositionTicks", 0)
            total = item.get("RunTimeTicks", 1)
            progress = f"{(pos / total * 100):.2f}%" if total > 0 else "0.00%"
            
            ip = session.get("RemoteEndPoint", "127.0.0.1")
            loc = self._get_location(ip)
            device = f"{session.get('Client','Emby')} {session.get('DeviceName','')}"

            emoji = "▶️" if action == "start" else "⏹️"
            act_txt = "开始播放" if action == "start" else "停止播放"

            msg = (
                f"{emoji} <b>【{user}】{act_txt} {title}</b>\n"
                f"📚 类型：{'剧集' if item.get('Type')=='Episode' else '电影'}\n"
                f"🔄 进度：{progress}\n"
                f"🌐 IP地址：{ip} {loc}\n"
                f"📱 设备：{device}\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            img = self._download_emby_image(item.get("Id"), 'Backdrop') or self._download_emby_image(item.get("Id"), 'Primary')
            if img: self.send_photo(cid, img, msg)
            else: self.send_message(cid, msg)
        except Exception as e:
            logger.error(f"Playback Push Error: {e}")

    def push_new_media(self, item_id):
        """针对 STRM 文件 404 问题的多重重试入库通知"""
        if not cfg.get("enable_library_notify") or not cfg.get("tg_chat_id"): return
        cid = str(cfg.get("tg_chat_id")); host = cfg.get("emby_host"); key = cfg.get("emby_api_key")
        
        item = None
        for i in range(3): # 最多等待 40 秒
            time.sleep(10 if i == 0 else 15) 
            try:
                res = requests.get(f"{host}/emby/Items/{item_id}?api_key={key}", timeout=10)
                if res.status_code == 200:
                    item = res.json()
                    break
                print(f"DEBUG: 资源 {item_id} 详情不可见({res.status_code})，正在进行第 {i+1} 次重试...")
            except: pass
        
        if not item: return

        try:
            name = item.get("Name", "")
            if item.get("Type") == "Episode":
                name = f"{item.get('SeriesName','')} S{str(item.get('ParentIndexNumber',1)).zfill(2)}E{str(item.get('IndexNumber',1)).zfill(2)}"
            
            overview = item.get("Overview", "暂无简介...")
            if len(overview) > 150: overview = overview[:140] + "..."
            
            caption = (
                f"📺 <b>新入库 {name}</b>\n"
                f"⭐ 评分：{item.get('CommunityRating','N/A')}/10 ｜ 📚 类型：{item.get('Type','影视')}\n"
                f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"📝 剧情：{overview}"
            )
            img = self._download_emby_image(item_id, 'Primary')
            # 降级：如果没海报，带占位图发送，确保通知必达
            self.send_photo(cid, img if img else REPORT_COVER_URL, caption)
        except Exception as e:
            logger.error(f"Library Push Error: {e}")

    # ================= 机器人指令系统 (全量恢复) =================
    def _set_commands(self):
        token = cfg.get("tg_bot_token")
        cmds = [
            {"command": "stats", "description": "📊 超级日报 (含排行图表)"},
            {"command": "now", "description": "🟢 当前正在播放详情"},
            {"command": "latest", "description": "🆕 最近入库 Top 5"},
            {"command": "recent", "description": "📜 最近 10 条播放动态"},
            {"command": "check", "description": "📡 服务器连接与 IP 检查"},
            {"command": "help", "description": "🤖 指令说明帮助"}
        ]
        try: requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", json={"commands": cmds}, proxies=self._get_proxies(), timeout=10)
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
                            cid = str(u["message"]["chat"]["id"])
                            if admin_id and cid != admin_id: 
                                self.send_message(cid, "🚫 未授权用户")
                                continue
                            self._handle_message(u["message"], cid)
                else: time.sleep(5)
            except: time.sleep(5)

    def _handle_message(self, msg, cid):
        text = msg.get("text", "").strip()
        if text.startswith("/stats"): self._cmd_stats(cid)
        elif text.startswith("/now"): self._cmd_now(cid)
        elif text.startswith("/latest"): self._cmd_latest(cid)
        elif text.startswith("/recent"): self._cmd_recent(cid)
        elif text.startswith("/check"): self._cmd_check(cid)
        elif text.startswith("/help"): self._cmd_help(cid)

    def _cmd_stats(self, cid):
        """生成详细日报"""
        where, params = get_base_filter('all')
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day')", params)[0]['c']
        # 活跃排行
        users = query_db(f"SELECT user_name, COUNT(*) as cnt FROM PlaybackActivity {where} AND DateCreated > date('now', 'start of day') AND user_name != '' GROUP BY user_name ORDER BY cnt DESC LIMIT 5", params)
        user_txt = "\n".join([f"🏆 {u['user_name']} ({u['cnt']}次)" for u in users]) if users else "暂无活跃数据"
        
        caption = f"📊 <b>今日媒体数据汇总</b>\n\n▶️ 今日播放：{plays} 次\n👥 活跃排行：\n{user_txt}"
        
        # 恢复图片日报逻辑
        if HAS_PIL:
            img = report_gen.generate_report('all', 'day')
            self.send_photo(cid, img, caption)
        else:
            self.send_photo(cid, REPORT_COVER_URL, caption)

    def _cmd_now(self, cid):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=5)
            sessions = [s for s in res.json() if s.get("NowPlayingItem")]
            if not sessions: 
                return self.send_message(cid, "🟢 服务器当前空闲中...") 
            
            msg = f"🟢 <b>正在播放 ({len(sessions)})</b>\n"
            for s in sessions:
                title = s['NowPlayingItem'].get('Name')
                user = s.get('UserName')
                pos = s.get('PlayState', {}).get('PositionTicks', 0)
                total = s['NowPlayingItem'].get('RunTimeTicks', 1)
                pct = int(pos / total * 100) if total > 0 else 0
                msg += f"\n👤 <b>{user}</b> | 🔄 {pct}%\n📺 {title}\n"
            self.send_message(cid, msg)
        except: self.send_message(cid, "❌ 暂时无法连接 Emby 获取会话")

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
        except: self.send_message(cid, "❌ 最近入库查询异常")

    def _cmd_recent(self, cid):
        rows = query_db("SELECT user_name, item_name, date_created FROM PlaybackActivity ORDER BY date_created DESC LIMIT 10")
        if not rows: return self.send_message(cid, "📭 播放历史记录为空")
        msg = "📜 <b>最近 10 条播放动态</b>\n"
        for r in rows:
            date_str = r['date_created'].split('T')[0][5:] if 'T' in r['date_created'] else r['date_created']
            time_str = r['date_created'].split('T')[1][:5] if 'T' in r['date_created'] else ""
            msg += f"\n⏰ {date_str} {time_str} | {r['user_name']}\n🎬 {r['item_name']}\n"
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
                msg = (
                    f"✅ <b>Emby 服务器连接正常</b>\n"
                    f"📡 响应延迟: {int((time.time()-start)*1000)}ms\n"
                    f"📦 版本号: {info.get('Version')}\n"
                    f"🏠 内网地址: {local}\n"
                    f"🌍 外网地址: {wan}"
                )
                self.send_message(cid, msg)
        except: self.send_message(cid, "❌ 连接 Emby 服务器失败")

    def _cmd_help(self, cid):
        msg = (
            "🤖 <b>EmbyPulse 指令指南</b>\n\n"
            "/stats - 查看今日统计及排行图表\n"
            "/now - 实时查看谁在看什么及进度\n"
            "/latest - 获取最近新添加的内容\n"
            "/recent - 回顾最近的历史记录\n"
            "/check - 诊断服务器连接与 IP 情况"
        )
        self.send_message(cid, msg)

    # ================= 原始多线程循环 (完全保留) =================

    def _monitor_loop(self):
        admin_id = str(cfg.get("tg_chat_id"))
        while self.running and cfg.get("enable_notify"):
            try:
                key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
                if not key or not host: time.sleep(30); continue
                res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=5)
                if res.status_code == 200:
                    current_ids = []
                    for s in res.json():
                        if s.get("NowPlayingItem"):
                            sid = s.get("Id"); current_ids.append(sid)
                            if sid not in self.active_sessions:
                                # 只有当 sid 真的不存在时才发送 (Webhook 之外的兜底)
                                self.active_sessions[sid] = True
                    stopped = [sid for sid in self.active_sessions if sid not in current_ids]
                    for sid in stopped: del self.active_sessions[sid]
                time.sleep(10)
            except: time.sleep(10)

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
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        for u in users:
            if u['expire_date'] < today:
                try: requests.post(f"{host}/emby/Users/{u['user_id']}/Policy?api_key={key}", json={"IsDisabled": True})
                except: pass

    def push_now(self, user_id, period, theme):
        if not cfg.get("tg_chat_id"): return False
        img = report_gen.generate_report(user_id, period, theme) if HAS_PIL else REPORT_COVER_URL
        self.send_photo(str(cfg.get("tg_chat_id")), img, f"🚀 <b>日报立即推送成功</b>")
        return True

bot = TelegramBot()
