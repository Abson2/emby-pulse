import threading
import time
import requests
import datetime
import io
import logging
import urllib.parse
import json 
import re
from collections import defaultdict
from app.core.config import cfg, REPORT_COVER_URL, FALLBACK_IMAGE_URL
from app.core.database import query_db, get_base_filter
from app.services.report_service import report_gen, HAS_PIL

logger = logging.getLogger("uvicorn")

class TelegramBot:
    def __init__(self):
        self.running = False
        self.poll_thread = None
        self.schedule_thread = None 
        self.library_queue = []
        self.library_lock = threading.Lock()
        self.library_thread = None
        
        self.offset = 0
        self.last_check_min = -1
        self.user_cache = {}
        
        self.wecom_token = None
        self.wecom_token_expires = 0
        
    def start(self):
        if self.running: return
        if not cfg.get("tg_bot_token") and not cfg.get("wecom_corpid"): return
        self.running = True
        
        self._set_commands()
        self._set_wecom_menu() 
        
        if cfg.get("tg_bot_token"):
            self.poll_thread = threading.Thread(target=self._polling_loop, daemon=True)
            self.poll_thread.start()
        
        self.schedule_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.schedule_thread.start()
        
        self.library_thread = threading.Thread(target=self._library_notify_loop, daemon=True)
        self.library_thread.start()
        
        print("🤖 Bot Service Started (Dual Channel Interactive Mode - V2 UI)")

    def stop(self): self.running = False

    def _get_proxies(self):
        proxy = cfg.get("proxy_url")
        return {"http": proxy, "https": proxy} if proxy else None

    def _get_admin_id(self):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return None
        try:
            res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
            if res.status_code == 200:
                users = res.json()
                for u in users:
                    if u.get("Policy", {}).get("IsAdministrator"): return u['Id']
                if users: return users[0]['Id']
        except: pass
        return None

    def _get_username(self, user_id):
        if user_id in self.user_cache: return self.user_cache[user_id]
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return user_id
        try:
            res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=2)
            if res.status_code == 200:
                for u in res.json(): self.user_cache[u['Id']] = u['Name']
        except: pass
        return self.user_cache.get(user_id, "Unknown User")

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
                url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=600&quality=90&tag={image_tag}"
            else:
                url = f"{host}/emby/Items/{item_id}/Images/{img_type}?maxHeight=800&maxWidth=600&quality=90&api_key={key}"
            res = requests.get(url, timeout=15)
            if res.status_code == 200: return io.BytesIO(res.content)
        except Exception as e: 
            logger.error(f"下载 Emby 海报失败: {str(e)}")
        return None

    # ================= 🔥 企微核心洗稿引擎 (格式大重构版) =================
    
    def _get_wecom_token(self):
        corpid = cfg.get("wecom_corpid"); corpsecret = cfg.get("wecom_corpsecret")
        proxy_url = cfg.get("wecom_proxy_url", "https://qyapi.weixin.qq.com").rstrip('/')
        if not corpid or not corpsecret: return None
        if self.wecom_token and time.time() < self.wecom_token_expires:
            return self.wecom_token
        try:
            res = requests.get(f"{proxy_url}/cgi-bin/gettoken?corpid={corpid}&corpsecret={corpsecret}", timeout=5).json()
            if res.get("errcode") == 0:
                self.wecom_token = res["access_token"]
                self.wecom_token_expires = time.time() + res["expires_in"] - 60
                return self.wecom_token
        except Exception as e: logger.error(f"WeCom Token Error: {e}")
        return None

    def _html_to_wecom_md(self, html_text, inline_keyboard=None):
        """洗稿引擎：专门为企业微信无图模式转化为 Markdown"""
        text = html_text.replace("<b>", "**").replace("</b>", "**")
        text = text.replace("<i>", "").replace("</i>", "")
        text = text.replace("<code>", "`").replace("</code>", "`")
        text = re.sub(r"<a\s+href=['\"](.*?)['\"]>(.*?)</a>", r"[\2](\1)", text)
        
        if inline_keyboard and "inline_keyboard" in inline_keyboard:
            text += "\n\n───────────────\n"
            for row in inline_keyboard["inline_keyboard"]:
                for btn in row:
                    if "text" in btn and "url" in btn:
                        text += f"> [{btn['text']}]({btn['url']})\n"
        return text.strip()

    def _set_wecom_menu(self):
        token = self._get_wecom_token(); agentid = cfg.get("wecom_agentid")
        proxy_url = cfg.get("wecom_proxy_url", "https://qyapi.weixin.qq.com").rstrip('/')
        if not token or not agentid: return
        menu_data = {
            "button": [
                {"type": "click", "name": "📊 数据日报", "key": "/stats"},
                {"type": "click", "name": "🟢 正在播放", "key": "/now"},
                {"name": "🎬 媒体库", "sub_button": [
                    {"type": "click", "name": "🆕 最近入库", "key": "/latest"},
                    {"type": "click", "name": "📜 播放记录", "key": "/recent"}
                ]}
            ]
        }
        try: 
            res = requests.post(f"{proxy_url}/cgi-bin/menu/create?access_token={token}&agentid={agentid}", json=menu_data, timeout=5)
            if res.status_code == 200: logger.info(f"WeCom Menu Sync OK")
        except Exception as e: logger.error(f"WeCom Menu Error: {e}")

    def _send_wecom_message(self, text, inline_keyboard=None, touser="@all"):
        """无图时发送 Markdown"""
        token = self._get_wecom_token(); agentid = cfg.get("wecom_agentid")
        proxy_url = cfg.get("wecom_proxy_url", "https://qyapi.weixin.qq.com").rstrip('/')
        if not token or not agentid: return
        try:
            md_text = self._html_to_wecom_md(text, inline_keyboard)
            url = f"{proxy_url}/cgi-bin/message/send?access_token={token}"
            requests.post(url, json={"touser": touser, "msgtype": "markdown", "agentid": int(agentid), "markdown": {"content": md_text}}, timeout=10)
        except Exception as e: pass

    def _send_wecom_photo(self, photo_bytes, html_text, inline_keyboard=None, touser="@all"):
        """有图时发送精美的 News 图文卡片"""
        token = self._get_wecom_token(); agentid = cfg.get("wecom_agentid")
        proxy_url = cfg.get("wecom_proxy_url", "https://qyapi.weixin.qq.com").rstrip('/')
        if not token or not agentid: return
        
        pic_url = REPORT_COVER_URL
        
        try:
            if photo_bytes:
                upload_url = f"{proxy_url}/cgi-bin/media/uploadimg?access_token={token}"
                files = {"media": ("image.jpg", photo_bytes, "image/jpeg")}
                upload_res = requests.post(upload_url, files=files, timeout=15)
                if upload_res.status_code == 200 and upload_res.text.strip():
                    try:
                        res_json = upload_res.json()
                        if res_json.get("url"): pic_url = res_json.get("url")
                    except Exception: pass
        except Exception as e: pass

        try:
            plain_text = re.sub(r'<[^>]+>', '', html_text)
            lines = [line.strip() for line in plain_text.split('\n') if line.strip()]
            
            title = lines[0] if lines else "EmbyPulse 通知"
            desc_lines = [line for line in lines[1:] if '─────' not in line]
            desc = '\n'.join(desc_lines)
            
            jump_url = cfg.get("emby_public_url") or cfg.get("emby_host") or "https://emby.media"
            if inline_keyboard and "inline_keyboard" in inline_keyboard:
                try: jump_url = inline_keyboard["inline_keyboard"][0][0]["url"]
                except: pass
            else:
                links = re.findall(r"href=['\"](.*?)['\"]", html_text)
                if links: jump_url = links[0]

            send_msg_url = f"{proxy_url}/cgi-bin/message/send?access_token={token}"
            msg_data = {
                "touser": touser,
                "msgtype": "news",
                "agentid": int(agentid),
                "news": {
                    "articles": [{
                        "title": title,
                        "description": desc,
                        "url": jump_url,
                        "picurl": pic_url
                    }]
                }
            }
            res = requests.post(send_msg_url, json=msg_data, timeout=10)
            if res.status_code == 200 and res.text.strip():
                try:
                    send_json = res.json()
                    if send_json.get("errcode", 0) != 0:
                        self._send_wecom_message(html_text, inline_keyboard, touser)
                except Exception:
                    self._send_wecom_message(html_text, inline_keyboard, touser)
            else:
                self._send_wecom_message(html_text, inline_keyboard, touser)
                
        except Exception as e:
            logger.error(f"WeCom News Error: {e}")
            if html_text: self._send_wecom_message(html_text, inline_keyboard, touser)

    # ================= 🚀 底层双通道路由分发 =================

    def send_photo(self, chat_id, photo_io, caption, parse_mode="HTML", reply_markup=None, platform="all"):
        photo_bytes = None
        if isinstance(photo_io, str):
            try: photo_bytes = requests.get(photo_io, timeout=10).content
            except Exception as e: pass
        else:
            photo_io.seek(0)
            photo_bytes = photo_io.read()

        if platform in ["all", "wecom"] and cfg.get("wecom_corpid"):
            touser = chat_id if platform == "wecom" else cfg.get("wecom_touser", "@all")
            threading.Thread(target=self._send_wecom_photo, args=(photo_bytes, caption, reply_markup, touser)).start()

        if platform in ["all", "tg"] and cfg.get("tg_bot_token"):
            tg_cid = chat_id if platform == "tg" else cfg.get("tg_chat_id")
            if tg_cid:
                try:
                    url = f"https://api.telegram.org/bot{cfg.get('tg_bot_token')}/sendPhoto"
                    data = {"chat_id": tg_cid, "caption": caption, "parse_mode": parse_mode}
                    if reply_markup: data["reply_markup"] = json.dumps(reply_markup)
                    if photo_bytes:
                        files = {"photo": ("image.jpg", io.BytesIO(photo_bytes), "image/jpeg")}
                        requests.post(url, data=data, files=files, proxies=self._get_proxies(), timeout=20)
                    else:
                        self.send_message(tg_cid, caption, parse_mode, reply_markup, platform="tg")
                except Exception as e: 
                    self.send_message(tg_cid, caption, parse_mode, reply_markup, platform="tg")

    def send_message(self, chat_id, text, parse_mode="HTML", reply_markup=None, platform="all"):
        if platform in ["all", "wecom"] and cfg.get("wecom_corpid"):
            touser = chat_id if platform == "wecom" else cfg.get("wecom_touser", "@all")
            threading.Thread(target=self._send_wecom_message, args=(text, reply_markup, touser)).start()

        if platform in ["all", "tg"] and cfg.get("tg_bot_token"):
            tg_cid = chat_id if platform == "tg" else cfg.get("tg_chat_id")
            if tg_cid:
                try:
                    url = f"https://api.telegram.org/bot{cfg.get('tg_bot_token')}/sendMessage"
                    data = {"chat_id": tg_cid, "text": text, "parse_mode": parse_mode}
                    if reply_markup: data["reply_markup"] = json.dumps(reply_markup)
                    requests.post(url, json=data, proxies=self._get_proxies(), timeout=10)
                except Exception as e: pass

    # ================= 业务排版逻辑 (V2 极致排版 + 智能连续区间) =================
    
    def add_library_task(self, item):
        with self.library_lock:
            if not any(x['Id'] == item['Id'] for x in self.library_queue):
                self.library_queue.append(item)

    def _library_notify_loop(self):
        while self.running:
            try:
                has_data = False
                with self.library_lock: has_data = len(self.library_queue) > 0
                if not has_data:
                    time.sleep(2)
                    continue

                time.sleep(15)
                items_to_process = []
                with self.library_lock:
                    items_to_process = self.library_queue[:]
                    self.library_queue = [] 
                
                if items_to_process: self._process_library_group(items_to_process)
            except Exception as e:
                time.sleep(5)

    def _process_library_group(self, items):
        if not cfg.get("enable_library_notify"): return
        
        groups = defaultdict(list)
        for item in items:
            itype = item.get('Type')
            if itype in ['Episode', 'Season'] and item.get('SeriesId'):
                sid = str(item.get('SeriesId'))
                groups[sid].append(item)
            elif itype == 'Series':
                sid = str(item.get('Id'))
                groups[sid].append(item)
            else:
                mid = str(item.get('Id'))
                groups[mid].append(item)

        for group_id, group_items in groups.items():
            try:
                episodes_only = [x for x in group_items if x.get('Type') == 'Episode']
                if len(episodes_only) > 0:
                    self._push_episode_group(group_id, episodes_only)
                elif len(group_items) == 1 and group_items[0].get('Type') == 'Series':
                    series_item = group_items[0]
                    fresh_episodes = self._check_fresh_episodes(group_id)
                    if fresh_episodes: self._push_episode_group(group_id, fresh_episodes)
                    else: self._push_single_item(series_item)
                else:
                    self._push_single_item(group_items[0])
                time.sleep(2) 
            except Exception as e: pass

    def _parse_emby_time(self, date_str):
        if not date_str: return None
        try:
            clean_str = date_str.replace('Z', '')[:26]
            if '.' in clean_str: return datetime.datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S.%f")
            else: return datetime.datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
        except: return None

    def _check_fresh_episodes(self, series_id):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        admin_id = self._get_admin_id()
        if not admin_id: return []
        
        try:
            url = f"{host}/emby/Users/{admin_id}/Items"
            params = {
                "ParentId": series_id, "Recursive": "true", "IncludeItemTypes": "Episode",
                "Limit": 20, "SortBy": "DateCreated", "SortOrder": "Descending",
                "Fields": "DateCreated,Name,ParentIndexNumber,IndexNumber", "api_key": key
            }
            res = requests.get(url, params=params, timeout=10)
            if res.status_code != 200: return []
            
            items = res.json().get("Items", [])
            if not items: return []

            fresh_list = []
            last_time = None

            for i, item in enumerate(items):
                curr_time = self._parse_emby_time(item.get("DateCreated"))
                if not curr_time: 
                    if i == 0: fresh_list.append(item)
                    break
                if i == 0:
                    fresh_list.append(item)
                    last_time = curr_time
                else:
                    delta = abs((last_time - curr_time).total_seconds())
                    if delta <= 60:
                        fresh_list.append(item)
                        last_time = curr_time 
                    else: break 
            return fresh_list
        except Exception as e: return []

    def _push_episode_group(self, series_id, episodes):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        admin_id = self._get_admin_id()
        
        series_info = {}
        try:
            url = f"{host}/emby/Users/{admin_id}/Items/{series_id}?api_key={key}"
            res = requests.get(url, timeout=10)
            if res.status_code == 200: series_info = res.json()
        except: pass
        if not series_info: series_info = episodes[0]

        episodes.sort(key=lambda x: (x.get('ParentIndexNumber', 1), x.get('IndexNumber', 1)))
        season_idx = episodes[0].get('ParentIndexNumber', 1)
        
        # 🔥🔥🔥 智能连续区间提取算法 🔥🔥🔥
        ep_indices = sorted(list(set([e.get('IndexNumber', 0) for e in episodes if e.get('IndexNumber') is not None])))

        if len(ep_indices) > 1:
            ranges = []
            start = ep_indices[0]
            end = ep_indices[0]
            
            for idx in ep_indices[1:]:
                if idx == end + 1:
                    end = idx
                else:
                    ranges.append(f"E{start}" if start == end else f"E{start}-E{end}")
                    start = idx
                    end = idx
            ranges.append(f"E{start}" if start == end else f"E{start}-E{end}")
            
            ep_range_str = ", ".join(ranges)
            title_suffix = f"新增 {len(ep_indices)} 集 ({ep_range_str})"
        elif len(ep_indices) == 1:
            title_suffix = f"E{str(ep_indices[0]).zfill(2)}"
            if episodes[0].get('Name') and "Episode" not in episodes[0].get('Name') and "第" not in episodes[0].get('Name'):
                title_suffix += f" {episodes[0].get('Name')}"
        else:
            title_suffix = f"新增 {len(episodes)} 集"

        series_name = series_info.get('Name', '未知剧集')
        year = series_info.get("ProductionYear", "")
        rating = series_info.get("CommunityRating", "N/A")
        overview = series_info.get("Overview", "暂无简介...") 
        if len(overview) > 150: overview = overview[:140] + "..."
        
        base_url = cfg.get("emby_public_url") or cfg.get("emby_host")
        if base_url.endswith('/'): base_url = base_url[:-1]
        play_url = f"{base_url}/web/index.html#!/item?id={series_id}&serverId={series_info.get('ServerId','')}"

        caption = (f"📺 <b>新入库 剧集</b>\n"
                   f"───────────────\n"
                   f"📌 <b>{series_name}</b> ({year})\n"
                   f"🏷 季集：S{str(season_idx).zfill(2)} {title_suffix}\n"
                   f"⭐ 评分：{rating} / 10\n"
                   f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                   f"───────────────\n"
                   f"📝 <b>剧情简介：</b>\n{overview}")

        keyboard = {"inline_keyboard": [[{"text": "▶️ 立即播放", "url": play_url}]]}

        img_io = self._download_emby_image(series_id, 'Primary')
        if not img_io: img_io = self._download_emby_image(series_id, 'Backdrop') 
        if not img_io: img_io = REPORT_COVER_URL
        self.send_photo("sys_notify", img_io, caption, reply_markup=keyboard, platform="all")

    def _push_single_item(self, item):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            url = f"{host}/emby/Items/{item['Id']}?api_key={key}"
            res = requests.get(url, timeout=10)
            if res.status_code == 200: item = res.json()
        except: pass

        name = item.get("Name", "未知")
        year = item.get("ProductionYear", "")
        rating = item.get("CommunityRating", "N/A")
        overview = item.get("Overview", "暂无简介...")
        if len(overview) > 150: overview = overview[:140] + "..."
        
        type_raw = item.get("Type")
        type_cn = "电影"; type_icon = "🎬"
        if type_raw in ["Series", "Episode"]: type_cn = "剧集"; type_icon = "📺"
        
        base_url = cfg.get("emby_public_url") or cfg.get("emby_host")
        if base_url.endswith('/'): base_url = base_url[:-1]
        play_url = f"{base_url}/web/index.html#!/item?id={item['Id']}&serverId={item.get('ServerId','')}"

        caption = (f"{type_icon} <b>新入库 {type_cn}</b>\n"
                   f"───────────────\n"
                   f"📌 <b>{name}</b> ({year})\n"
                   f"⭐ 评分：{rating} / 10\n"
                   f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                   f"───────────────\n"
                   f"📝 <b>剧情简介：</b>\n{overview}")
        
        keyboard = {"inline_keyboard": [[{"text": "▶️ 立即播放", "url": play_url}]]}

        img_io = self._download_emby_image(item['Id'], 'Primary')
        if not img_io: img_io = REPORT_COVER_URL
        self.send_photo("sys_notify", img_io, caption, reply_markup=keyboard, platform="all")

    def push_playback_event(self, data, action="start"):
        if not cfg.get("enable_notify"): return
        try:
            user = data.get("User", {})
            item = data.get("Item", {})
            session = data.get("Session", {})
            
            title = item.get("Name", "未知内容")
            ep_info = ""
            if item.get("SeriesName"): 
                idx = item.get("IndexNumber", 0); parent_idx = item.get("ParentIndexNumber", 1)
                ep_info = f"\n🏷 季集：S{str(parent_idx).zfill(2)}E{str(idx).zfill(2)} 第 {idx} 集"
                title = f"{item.get('SeriesName')}"
            
            type_cn = "剧集" if item.get("Type") == "Episode" else "电影"
            emoji = "▶️" if action == "start" else "⏹️"; act = "开始播放" if action == "start" else "停止播放"
            ip = session.get("RemoteEndPoint", "127.0.0.1"); loc = self._get_location(ip)
            
            msg = (f"{emoji} <b>【{user.get('Name')}】{act}</b>\n"
                   f"───────────────\n"
                   f"🎬 <b>{title}</b>{ep_info}\n"
                   f"📚 类型：{type_cn}\n"
                   f"🌐 地址：{ip} ({loc})\n"
                   f"📱 设备：{session.get('Client')} on {session.get('DeviceName')}\n"
                   f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            target_id = item.get("Id")
            if item.get("Type") == "Episode" and item.get("SeriesId"): target_id = item.get("SeriesId")
            
            base_url = cfg.get("emby_public_url") or cfg.get("emby_host")
            if base_url.endswith('/'): base_url = base_url[:-1]
            play_url = f"{base_url}/web/index.html#!/item?id={target_id}&serverId={item.get('ServerId','')}"
            keyboard = {"inline_keyboard": [[{"text": "🔗 跳转详情", "url": play_url}]]}

            img_io = self._download_emby_image(target_id, 'Primary') 
            if not img_io: img_io = self._download_emby_image(item.get("Id"), 'Backdrop')
            if not img_io: img_io = REPORT_COVER_URL
            
            self.send_photo("sys_notify", img_io, msg, reply_markup=keyboard, platform="all")
        except Exception as e:
            logger.error(f"Playback Push Error: {e}")

    # ================= 指令系统 =================

    def _set_commands(self):
        token = cfg.get("tg_bot_token")
        if not token: return
        cmds = [{"command": "search", "description": "🔍 搜索资源"},
                {"command": "stats", "description": "📊 今日日报"},
                {"command": "weekly", "description": "📅 本周周报"},
                {"command": "monthly", "description": "🗓️ 本月月报"},
                {"command": "yearly", "description": "📜 年度总结"},
                {"command": "now", "description": "🟢 正在播放"},
                {"command": "latest", "description": "🆕 最近入库"},
                {"command": "recent", "description": "📜 播放历史"},
                {"command": "check", "description": "📡 系统检查"},
                {"command": "help", "description": "🤖 帮助菜单"}]
        try: requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", json={"commands": cmds}, proxies=self._get_proxies(), timeout=10)
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
                            cid = str(u["message"]["chat"]["id"]); 
                            if admin_id and cid != admin_id: continue
                            text = u["message"].get("text", "")
                            self._handle_message(text, cid, platform="tg")
                else: time.sleep(5)
            except: time.sleep(5)

    def _handle_message(self, text, cid, platform="tg"):
        text = text.strip()
        if text.startswith("/search"): self._cmd_search(cid, text, platform)
        elif text.startswith("/stats"): self._cmd_stats(cid, 'day', platform)
        elif text.startswith("/weekly"): self._cmd_stats(cid, 'week', platform)
        elif text.startswith("/monthly"): self._cmd_stats(cid, 'month', platform)
        elif text.startswith("/yearly"): self._cmd_stats(cid, 'year', platform)
        elif text.startswith("/now"): self._cmd_now(cid, platform)
        elif text.startswith("/latest"): self._cmd_latest(cid, platform)
        elif text.startswith("/recent"): self._cmd_recent(cid, platform)
        elif text.startswith("/check"): self._cmd_check(cid, platform)
        elif text.startswith("/help"): self._cmd_help(cid, platform)

    def _cmd_latest(self, cid, platform):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            user_id = self._get_admin_id()
            if not user_id: return self.send_message(cid, "❌ 错误: 无法获取 Emby 用户身份", platform=platform)
            fields = "DateCreated,Name,SeriesName,ProductionYear,Type"
            url = f"{host}/emby/Users/{user_id}/Items/Latest"
            params = {"Limit": 8, "MediaTypes": "Video", "Fields": fields, "api_key": key}
            res = requests.get(url, params=params, timeout=15)
            if res.status_code != 200: return self.send_message(cid, f"❌ 查询失败", platform=platform)
            items = res.json()
            if not items: return self.send_message(cid, "📭 最近没有新入库的资源", platform=platform)

            msg = "🆕 <b>最近入库 (Top 8)</b>\n───────────────\n"
            count = 0
            for i in items:
                if count >= 8: break
                if i.get("Type") not in ["Movie", "Series", "Episode"]: continue
                name = i.get("Name")
                if i.get("SeriesName"): name = f"{i.get('SeriesName')} - {name}"
                date_str = i.get("DateCreated", "")[:10]
                type_icon = "🎬" if i.get("Type") == "Movie" else "📺"
                msg += f"{type_icon} {date_str} | <b>{name}</b>\n"
                count += 1
            self.send_message(cid, msg.strip(), platform=platform)
        except Exception as e:
            self.send_message(cid, f"❌ 查询异常", platform=platform)

    def _extract_tech_info(self, item):
        sources = item.get("MediaSources", [])
        if not sources: return "📼 未知"
        info_parts = []
        video = next((s for s in sources[0].get("MediaStreams", []) if s.get("Type") == "Video"), None)
        if video:
            w = video.get("Width", 0)
            if w >= 3800: res = "4K"
            elif w >= 1900: res = "1080P"
            elif w >= 1200: res = "720P"
            else: res = "SD"
            extra = []
            v_range = video.get("VideoRange", "")
            title = video.get("DisplayTitle", "").upper()
            if "HDR" in v_range or "HDR" in title: extra.append("HDR")
            if "DOVI" in title or "DOLBY VISION" in title: extra.append("DoVi")
            res_str = f"{res} {' '.join(extra)}"
            info_parts.append(res_str.strip())
            bitrate = sources[0].get("Bitrate", 0)
            if bitrate > 0: info_parts.append(f"{round(bitrate / 1000000, 1)}Mbps")
        return " | ".join(info_parts) if info_parts else "📼 未知"

    def _cmd_search(self, chat_id, text, platform):
        parts = text.split(' ', 1)
        if len(parts) < 2: return self.send_message(chat_id, "🔍 请使用: /search 关键词", platform=platform)
        keyword = parts[1].strip()
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            user_id = self._get_admin_id()
            if not user_id: return self.send_message(chat_id, "❌ 错误: 无法获取 Emby 用户身份", platform=platform)

            fields = "ProductionYear,Type,Id" 
            url = f"{host}/emby/Users/{user_id}/Items"
            params = {"SearchTerm": keyword, "IncludeItemTypes": "Movie,Series", "Recursive": "true", "Fields": fields, "Limit": 5, "api_key": key}
            res = requests.get(url, params=params, timeout=10)
            if res.status_code != 200: return self.send_message(chat_id, f"❌ 搜索失败", platform=platform)
            items = res.json().get("Items", [])
            if not items: return self.send_message(chat_id, f"📭 未找到与 <b>{keyword}</b> 相关的资源", platform=platform)
            
            top = items[0]
            type_raw = top.get("Type")
            tech_info_str = "查询中..."; ep_count_str = ""; details = {}

            try:
                if type_raw == "Series":
                    meta_url = f"{host}/emby/Users/{user_id}/Items/{top['Id']}?Fields=Overview,CommunityRating,Genres,RecursiveItemCount&api_key={key}"
                    details = requests.get(meta_url, timeout=5).json()
                    ep_count = details.get("RecursiveItemCount", 0)
                    ep_count_str = f"📊 共 {ep_count} 集"
                    sample_url = f"{host}/emby/Users/{user_id}/Items?ParentId={top['Id']}&Recursive=true&IncludeItemTypes=Episode&Limit=1&Fields=MediaSources&api_key={key}"
                    sample_res = requests.get(sample_url, timeout=5)
                    if sample_res.status_code == 200 and sample_res.json().get("Items"):
                        tech_info_str = self._extract_tech_info(sample_res.json().get("Items")[0])
                else:
                    detail_url = f"{host}/emby/Users/{user_id}/Items/{top['Id']}?Fields=Overview,CommunityRating,Genres,MediaSources&api_key={key}"
                    details = requests.get(detail_url, timeout=8).json()
                    tech_info_str = self._extract_tech_info(details)
            except Exception: tech_info_str = "暂无技术信息"

            name = details.get("Name", top.get("Name"))
            year = details.get("ProductionYear", top.get("ProductionYear"))
            year_str = f"({year})" if year else ""
            rating = details.get("CommunityRating", "N/A")
            genres = " / ".join(details.get("Genres", [])[:3]) or "未分类"
            overview = details.get("Overview", "暂无简介")
            if len(overview) > 120: overview = overview[:120] + "..."
            
            type_icon = "🎬" if type_raw == "Movie" else "📺"
            info_line = f"{ep_count_str} | {tech_info_str}" if type_raw == "Series" else tech_info_str
            
            base_url = cfg.get("emby_public_url") or cfg.get("emby_public_host") or host
            if base_url.endswith('/'): base_url = base_url[:-1]
            play_url = f"{base_url}/web/index.html#!/item?id={top.get('Id')}&serverId={top.get('ServerId')}"

            caption = (f"{type_icon} <b>{name}</b> {year_str}\n"
                       f"⭐️ {rating}  |  🎭 {genres}\n"
                       f"💿 {info_line}\n"
                       f"───────────────\n"
                       f"📝 <b>剧情简介：</b>\n{overview}\n")
            
            if len(items) > 1:
                caption += "───────────────\n🔎 <b>其他结果：</b>\n"
                for i, sub in enumerate(items[1:]):
                    sub_year = f"({sub.get('ProductionYear')})" if sub.get('ProductionYear') else ""
                    sub_type = "📺" if sub.get("Type") == "Series" else "🎬"
                    caption += f"{sub_type} {sub.get('Name')} {sub_year}\n"
            
            keyboard = {"inline_keyboard": [[{"text": "▶️ 立即播放", "url": play_url}]]}
            img_io = self._download_emby_image(top.get("Id"), 'Primary')
            
            if not img_io: img_io = REPORT_COVER_URL
            self.send_photo(chat_id, img_io, caption.strip(), reply_markup=keyboard, platform=platform)
        except Exception as e:
            self.send_message(chat_id, "❌ 搜索时发生错误", platform=platform)

    def _cmd_stats(self, chat_id, period='day', platform="tg"):
        where, params = get_base_filter('all') 
        titles = {'day': '今日日报', 'yesterday': '昨日日报', 'week': '本周周报', 'month': '本月月报', 'year': '年度报告'}
        title_cn = titles.get(period, '数据报表')
        if period == 'week': where += " AND DateCreated > date('now', '-7 days')"
        elif period == 'month': where += " AND DateCreated > date('now', 'start of month')"
        elif period == 'year': where += " AND DateCreated > date('now', 'start of year')"
        elif period == 'yesterday': where += " AND DateCreated >= date('now', '-1 day', 'start of day') AND DateCreated < date('now', 'start of day')"
        else: where += " AND DateCreated > date('now', 'start of day')"
        try:
            plays_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)
            if not plays_res: raise Exception("DB Error")
            plays = plays_res[0]['c']
            dur_res = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
            dur = dur_res[0]['c'] if dur_res and dur_res[0]['c'] else 0
            hours = round(dur / 3600, 1)
            users_res = query_db(f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where}", params)
            users = users_res[0]['c'] if users_res else 0
            
            top_users = query_db(f"SELECT UserId, SUM(PlayDuration) as t FROM PlaybackActivity {where} GROUP BY UserId ORDER BY t DESC LIMIT 5", params)
            user_str = ""
            if top_users:
                for i, u in enumerate(top_users):
                    name = self._get_username(u['UserId'])
                    h = round(u['t'] / 3600, 1)
                    prefix = ['🥇','🥈','🥉'][i] if i < 3 else f"{i+1}."
                    user_str += f"{prefix} {name} ({h}h)\n"
            else: user_str = "暂无数据\n"
            
            tops = query_db(f"SELECT ItemName, COUNT(*) as c FROM PlaybackActivity {where} GROUP BY ItemName ORDER BY c DESC LIMIT 10", params)
            top_content = ""
            if tops:
                for i, item in enumerate(tops):
                    prefix = ['🥇','🥈','🥉'][i] if i < 3 else f"{i+1}."
                    top_content += f"{prefix} {item['ItemName']} ({item['c']}次)\n"
            else: top_content = "暂无数据\n"
            
            yesterday_date = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%m-%d")
            title_display = f"{title_cn} ({yesterday_date})" if period == 'yesterday' else title_cn
            
            caption = (f"📊 <b>EmbyPulse {title_display}</b>\n"
                       f"───────────────\n"
                       f"📈 <b>数据大盘</b>\n"
                       f"▶️ 总播放量：{plays} 次\n"
                       f"⏱️ 活跃时长：{hours} 小时\n"
                       f"👥 活跃人数：{users} 人\n"
                       f"───────────────\n"
                       f"🏆 <b>活跃用户 Top 5</b>\n"
                       f"{user_str}"
                       f"───────────────\n"
                       f"🔥 <b>热门内容 Top 10</b>\n"
                       f"{top_content}")
            
            if HAS_PIL:
                img = report_gen.generate_report('all', period)
                if img: self.send_photo(chat_id, img, caption.strip(), platform=platform)
                else: self.send_message(chat_id, caption.strip(), platform=platform)
            else: self.send_photo(chat_id, REPORT_COVER_URL, caption.strip(), platform=platform)
        except Exception as e:
            self.send_message(chat_id, f"❌ 统计失败", platform=platform)

    def _daily_report_task(self):
        chat_id = "sys_notify"
        where = "WHERE DateCreated >= date('now', '-1 day', 'start of day') AND DateCreated < date('now', 'start of day')"
        res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}")
        count = res[0]['c'] if res else 0
        if count == 0:
            yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            msg = (f"📅 <b>昨日日报 ({yesterday_str})</b>\n"
                   f"───────────────\n"
                   f"😴 昨天服务器静悄悄，大家都去现充了吗？\n\n"
                   f"📊 活跃用户：0 人\n"
                   f"⏳ 播放时长：0 小时")
            self.send_message(chat_id, msg, platform="all")
        else: self._cmd_stats(chat_id, 'yesterday', platform="all")

    def _cmd_now(self, cid, platform):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            res = requests.get(f"{host}/emby/Sessions?api_key={key}", timeout=5)
            sessions = [s for s in res.json() if s.get("NowPlayingItem")]
            if not sessions: return self.send_message(cid, "🟢 当前无播放", platform=platform)
            
            msg = f"🟢 <b>当前正在播放 ({len(sessions)})</b>\n───────────────\n"
            for s in sessions:
                title = s['NowPlayingItem'].get('Name')
                pct = int(s.get('PlayState', {}).get('PositionTicks', 0) / s['NowPlayingItem'].get('RunTimeTicks', 1) * 100)
                msg += f"👤 <b>{s.get('UserName')}</b>  [ 🔄 {pct}% ]\n📺 {title}\n\n"
            self.send_message(cid, msg.strip(), platform=platform)
        except: self.send_message(cid, "❌ 连接失败", platform=platform)

    def _cmd_recent(self, cid, platform):
        try:
            rows = query_db("SELECT UserId, ItemName, DateCreated FROM PlaybackActivity ORDER BY DateCreated DESC LIMIT 10")
            if not rows: return self.send_message(cid, "📭 无记录", platform=platform)
            
            msg = "📜 <b>最近播放记录 (Top 10)</b>\n───────────────\n"
            for r in rows:
                date = r['DateCreated'][:16].replace('T', ' ')
                name = self._get_username(r['UserId'])
                msg += f"👤 <b>{name}</b> | ⏰ {date}\n🎬 {r['ItemName']}\n\n"
            self.send_message(cid, msg.strip(), platform=platform)
        except Exception as e: 
            self.send_message(cid, f"❌ 查询失败", platform=platform)

    def _cmd_check(self, cid, platform):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        start = time.time()
        try:
            res = requests.get(f"{host}/emby/System/Info?api_key={key}", timeout=5)
            if res.status_code == 200:
                info = res.json()
                local = (info.get('LocalAddresses') or [info.get('LocalAddress')])[0]
                wan = (info.get('RemoteAddresses') or [info.get('WanAddress')])[0]
                
                msg = (f"✅ <b>Emby 服务器状态：在线</b>\n"
                       f"───────────────\n"
                       f"⚡️ 响应延迟：{int((time.time()-start)*1000)} ms\n"
                       f"🏠 内网地址：{local}\n"
                       f"🌍 外网地址：{wan}")
                self.send_message(cid, msg, platform=platform)
        except: self.send_message(cid, "❌ 离线", platform=platform)

    def _cmd_help(self, cid, platform):
        self.send_message(cid, "🤖 /search, /stats, /weekly, /monthly, /now, /latest, /recent, /check", platform=platform)

    def _scheduler_loop(self):
        while self.running:
            try:
                now = datetime.datetime.now()
                if now.minute != self.last_check_min:
                    self.last_check_min = now.minute
                    if now.hour == 9 and now.minute == 0:
                        self._check_user_expiration()
                        if cfg.get("tg_chat_id") or cfg.get("wecom_corpid"): 
                            self._daily_report_task()
                time.sleep(5)
            except: time.sleep(60)

    def _check_user_expiration(self):
        try:
            users = query_db("SELECT user_id, expire_date FROM users_meta WHERE expire_date IS NOT NULL AND expire_date != ''")
            if not users: return
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
            for u in users:
                if u['expire_date'] < today:
                    try: requests.post(f"{host}/emby/Users/{u['user_id']}/Policy?api_key={key}", json={"IsDisabled": True})
                    except: pass
        except: pass
    
    def push_now(self, user_id, period, theme):
        self._cmd_stats("sys_notify", period, platform="all")
        return True

bot = TelegramBot()