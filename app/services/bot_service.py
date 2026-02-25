import threading
import time
import requests
import datetime
import io
import logging
import urllib.parse
import json 
from collections import defaultdict
# from dateutil import parser # ❌ 移除这个库
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
        
    def start(self):
        if self.running: return
        if not cfg.get("tg_bot_token"): return
        self.running = True
        self._set_commands()
        
        self.poll_thread = threading.Thread(target=self._polling_loop, daemon=True)
        self.poll_thread.start()
        
        self.schedule_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.schedule_thread.start()
        
        self.library_thread = threading.Thread(target=self._library_notify_loop, daemon=True)
        self.library_thread.start()
        
        print("🤖 Bot Service Started (Cluster Mode - Native)")

    def stop(self): self.running = False

    def _get_proxies(self):
        proxy = cfg.get("proxy_url")
        return {"http": proxy, "https": proxy} if proxy else None

    # 获取管理员ID
    def _get_admin_id(self):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return None
        try:
            res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
            if res.status_code == 200:
                users = res.json()
                for u in users:
                    if u.get("Policy", {}).get("IsAdministrator"):
                        return u['Id']
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
        except: pass
        return None

    def send_photo(self, chat_id, photo_io, caption, parse_mode="HTML", reply_markup=None):
        token = cfg.get("tg_bot_token")
        if not token: return
        try:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode}
            if reply_markup: data["reply_markup"] = json.dumps(reply_markup)
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
        except Exception as e: logger.error(f"Send Message Error: {e}")

    # ================= 🚀 修复后的入库逻辑 (时间聚类算法 - 原生版) =================
    
    def add_library_task(self, item):
        with self.library_lock:
            if not any(x['Id'] == item['Id'] for x in self.library_queue):
                self.library_queue.append(item)

    def _library_notify_loop(self):
        while self.running:
            try:
                has_data = False
                with self.library_lock:
                    has_data = len(self.library_queue) > 0
                
                if not has_data:
                    time.sleep(2)
                    continue

                time.sleep(15)

                items_to_process = []
                with self.library_lock:
                    items_to_process = self.library_queue[:]
                    self.library_queue = [] 
                
                if items_to_process:
                    self._process_library_group(items_to_process)
                    
            except Exception as e:
                logger.error(f"Library Loop Error: {e}")
                time.sleep(5)

    def _process_library_group(self, items):
        if not cfg.get("enable_library_notify") or not cfg.get("tg_chat_id"): return
        
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
                    
                    if fresh_episodes:
                        logger.info(f"🔄 捕获到 Series {group_id} 的 {len(fresh_episodes)} 个新集数 (主动回查)")
                        self._push_episode_group(group_id, fresh_episodes)
                    else:
                        self._push_single_item(series_item)
                else:
                    self._push_single_item(group_items[0])
                
                time.sleep(2) 
            except Exception as e:
                logger.error(f"Group Process Error: {e}")

    # 🔥 新增：原生时间解析函数
    def _parse_emby_time(self, date_str):
        if not date_str: return None
        try:
            # 去掉可能的 Z 后缀，截取前26位 (微秒部分)
            # Emby 格式: 2024-02-24T18:00:00.1234567Z
            clean_str = date_str.replace('Z', '')[:26]
            if '.' in clean_str:
                return datetime.datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S.%f")
            else:
                return datetime.datetime.strptime(clean_str, "%Y-%m-%dT%H:%M:%S")
        except:
            return None

    # 🔥 修复：使用原生解析
    def _check_fresh_episodes(self, series_id):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        admin_id = self._get_admin_id()
        if not admin_id: return []
        
        try:
            url = f"{host}/emby/Users/{admin_id}/Items"
            params = {
                "ParentId": series_id,
                "Recursive": "true",
                "IncludeItemTypes": "Episode",
                "Limit": 20, 
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "Fields": "DateCreated,Name,ParentIndexNumber,IndexNumber",
                "api_key": key
            }
            res = requests.get(url, params=params, timeout=10)
            if res.status_code != 200: return []
            
            items = res.json().get("Items", [])
            if not items: return []

            fresh_list = []
            last_time = None

            for i, item in enumerate(items):
                curr_time = self._parse_emby_time(item.get("DateCreated"))
                
                if not curr_time: # 解析失败
                    if i == 0: fresh_list.append(item)
                    break

                if i == 0:
                    fresh_list.append(item)
                    last_time = curr_time
                else:
                    # 计算间隔 (秒)
                    delta = abs((last_time - curr_time).total_seconds())
                    
                    if delta <= 60:
                        fresh_list.append(item)
                        last_time = curr_time 
                    else:
                        break # 断层
            
            return fresh_list
        except Exception as e:
            logger.error(f"Check Fresh Episodes Error: {e}")
            return []

    def _push_episode_group(self, series_id, episodes):
        cid = str(cfg.get("tg_chat_id"))
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
        ep_indices = [e.get('IndexNumber', 0) for e in episodes]
        
        ep_indices = sorted(list(set(ep_indices)))

        if len(ep_indices) > 1:
            ep_range = f"E{min(ep_indices)} - E{max(ep_indices)}"
            title_suffix = f"新增 {len(ep_indices)} 集 ({ep_range})"
        else:
            title_suffix = f"E{str(ep_indices[0]).zfill(2)}"
            if episodes[0].get('Name') and "Episode" not in episodes[0].get('Name'):
                title_suffix += f" {episodes[0].get('Name')}"

        display_title = f"{series_info.get('Name')} S{str(season_idx).zfill(2)} {title_suffix}"
        
        year = series_info.get("ProductionYear", "")
        rating = series_info.get("CommunityRating", "N/A")
        overview = series_info.get("Overview", "暂无简介...") 
        if len(overview) > 150: overview = overview[:140] + "..."
        
        caption = (f"📺 <b>新入库 剧集</b>\n{display_title} ({year})\n\n"
                   f"⭐ 评分：{rating}/10\n"
                   f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                   f"📝 剧情：{overview}")

        img_io = self._download_emby_image(series_id, 'Primary')
        if not img_io: img_io = self._download_emby_image(series_id, 'Backdrop') 
        
        if img_io: self.send_photo(cid, img_io, caption)
        else: self.send_photo(cid, REPORT_COVER_URL, caption)

    def _push_single_item(self, item):
        cid = str(cfg.get("tg_chat_id"))
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
        type_cn = "电影"
        type_icon = "🎬"
        
        if type_raw == "Series":
            type_cn = "剧集"
            type_icon = "📺"
        elif type_raw == "Episode":
            type_cn = "剧集"
            type_icon = "📺"
        
        caption = (f"{type_icon} <b>新入库 {type_cn}</b>\n{name} ({year})\n\n"
                   f"⭐ 评分：{rating}/10\n"
                   f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                   f"📝 剧情：{overview}")
        
        img_io = self._download_emby_image(item['Id'], 'Primary')
        if img_io: self.send_photo(cid, img_io, caption)
        else: self.send_photo(cid, REPORT_COVER_URL, caption)

    # ================= 业务逻辑 (保持不变) =================

    def push_playback_event(self, data, action="start"):
        if not cfg.get("enable_notify") or not cfg.get("tg_chat_id"): return
        try:
            chat_id = str(cfg.get("tg_chat_id"))
            user = data.get("User", {})
            item = data.get("Item", {})
            session = data.get("Session", {})
            
            title = item.get("Name", "未知内容")
            if item.get("SeriesName"): 
                idx = item.get("IndexNumber", 0); parent_idx = item.get("ParentIndexNumber", 1)
                title = f"{item.get('SeriesName')} S{str(parent_idx).zfill(2)}E{str(idx).zfill(2)} {title}"
            
            type_cn = "剧集" if item.get("Type") == "Episode" else "电影"
            emoji = "▶️" if action == "start" else "⏹️"; act = "开始播放" if action == "start" else "停止播放"
            ip = session.get("RemoteEndPoint", "127.0.0.1"); loc = self._get_location(ip)
            
            msg = (f"{emoji} <b>【{user.get('Name')}】{act}</b>\n"
                   f"📺 {title}\n"
                   f"📚 类型：{type_cn}\n"
                   f"🌐 地址：{ip} ({loc})\n"
                   f"📱 设备：{session.get('Client')} on {session.get('DeviceName')}\n"
                   f"🕒 时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            target_id = item.get("Id")
            if item.get("Type") == "Episode" and item.get("SeriesId"): target_id = item.get("SeriesId")
            
            img_io = self._download_emby_image(target_id, 'Primary') 
            if not img_io: img_io = self._download_emby_image(item.get("Id"), 'Backdrop')
            
            if img_io: self.send_photo(chat_id, img_io, msg)
            else: self.send_message(chat_id, msg)
        except Exception as e:
            logger.error(f"Playback Push Error: {e}")

    # ================= 指令系统 (保持不变) =================

    def _set_commands(self):
        token = cfg.get("tg_bot_token")
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
                            self._handle_message(u["message"], cid)
                else: time.sleep(5)
            except: time.sleep(5)

    def _handle_message(self, msg, cid):
        text = msg.get("text", "").strip()
        if text.startswith("/search"): self._cmd_search(cid, text)
        elif text.startswith("/stats"): self._cmd_stats(cid, 'day')
        elif text.startswith("/weekly"): self._cmd_stats(cid, 'week')
        elif text.startswith("/monthly"): self._cmd_stats(cid, 'month')
        elif text.startswith("/yearly"): self._cmd_stats(cid, 'year')
        elif text.startswith("/now"): self._cmd_now(cid)
        elif text.startswith("/latest"): self._cmd_latest(cid)
        elif text.startswith("/recent"): self._cmd_recent(cid)
        elif text.startswith("/check"): self._cmd_check(cid)
        elif text.startswith("/help"): self._cmd_help(cid)

    def _cmd_latest(self, cid):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            user_id = self._get_admin_id()
            if not user_id: return self.send_message(cid, "❌ 错误: 无法获取 Emby 用户身份")

            fields = "DateCreated,Name,SeriesName,ProductionYear,Type"
            url = f"{host}/emby/Users/{user_id}/Items/Latest"
            params = {"Limit": 8, "MediaTypes": "Video", "Fields": fields, "api_key": key}
            
            res = requests.get(url, params=params, timeout=15)
            if res.status_code != 200: return self.send_message(cid, f"❌ 查询失败: Emby 返回 HTTP {res.status_code}")

            items = res.json()
            if not items: return self.send_message(cid, "📭 最近没有新入库的资源")

            msg = "🆕 <b>最近入库</b>\n"
            count = 0
            for i in items:
                if count >= 8: break
                if i.get("Type") not in ["Movie", "Series", "Episode"]: continue
                name = i.get("Name")
                if i.get("SeriesName"): name = f"{i.get('SeriesName')} - {name}"
                date_str = i.get("DateCreated", "")[:10]
                type_icon = "🎬" if i.get("Type") == "Movie" else "📺"
                msg += f"\n{type_icon} {date_str} | {name}"
                count += 1
            self.send_message(cid, msg)
        except Exception as e:
            self.send_message(cid, f"❌ 查询异常: {str(e)}")

    def _extract_tech_info(self, item):
        sources = item.get("MediaSources", [])
        if not sources: return "📼 未知信息"
        
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
            
            res_str = f"{res}"
            if extra: res_str += f" {' '.join(extra)}"
            info_parts.append(res_str)
            
            bitrate = sources[0].get("Bitrate", 0)
            if bitrate > 0:
                mbps = round(bitrate / 1000000, 1)
                info_parts.append(f"{mbps}Mbps")
                
        return " | ".join(info_parts) if info_parts else "📼 未知信息"

    def _cmd_search(self, chat_id, text):
        parts = text.split(' ', 1)
        if len(parts) < 2: return self.send_message(chat_id, "🔍 <b>搜索格式错误</b>\n请使用: <code>/search 关键词</code>")
        keyword = parts[1].strip()
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        
        try:
            user_id = self._get_admin_id()
            if not user_id: return self.send_message(cid, "❌ 错误: 无法获取 Emby 用户身份")

            # 1️⃣ 第一步：只搜基础信息
            fields = "ProductionYear,Type,Id" 
            url = f"{host}/emby/Users/{user_id}/Items"
            params = {
                "SearchTerm": keyword,
                "IncludeItemTypes": "Movie,Series",
                "Recursive": "true",
                "Fields": fields,
                "Limit": 5,
                "api_key": key
            }
            res = requests.get(url, params=params, timeout=10)
            if res.status_code != 200: return self.send_message(chat_id, f"❌ 搜索失败 (HTTP {res.status_code})")
            
            items = res.json().get("Items", [])
            if not items: return self.send_message(chat_id, f"📭 未找到与 <b>{keyword}</b> 相关的资源")
            
            # 2️⃣ 第二步：查询详细信息
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
                        sample_ep = sample_res.json().get("Items")[0]
                        tech_info_str = self._extract_tech_info(sample_ep)
                else:
                    detail_url = f"{host}/emby/Users/{user_id}/Items/{top['Id']}?Fields=Overview,CommunityRating,Genres,MediaSources&api_key={key}"
                    details = requests.get(detail_url, timeout=8).json()
                    tech_info_str = self._extract_tech_info(details)
            except Exception as e:
                logger.error(f"Detail Fetch Error: {e}")
                tech_info_str = "暂无技术信息"

            # 3️⃣ 组装消息
            name = details.get("Name", top.get("Name"))
            year = details.get("ProductionYear", top.get("ProductionYear"))
            year_str = f"({year})" if year else ""
            rating = details.get("CommunityRating", "N/A")
            genres = " / ".join(details.get("Genres", [])[:3]) or "未分类"
            overview = details.get("Overview", "暂无简介")
            if len(overview) > 120: overview = overview[:120] + "..."
            
            type_icon = "🎬" if type_raw == "Movie" else "📺"
            info_line = tech_info_str
            if type_raw == "Series": info_line = f"{ep_count_str} | {tech_info_str}"
            
            caption = (f"{type_icon} <b>{name}</b> {year_str}\n"
                       f"⭐️ {rating}  |  🎭 {genres}\n"
                       f"{info_line}\n"
                       f"───────────────\n"
                       f"📝 <b>简介</b>: {overview}\n")
            
            if len(items) > 1:
                caption += "\n🔎 <b>其他结果:</b>\n"
                for i, sub in enumerate(items[1:]):
                    sub_year = f"({sub.get('ProductionYear')})" if sub.get('ProductionYear') else ""
                    sub_type = "📺" if sub.get("Type") == "Series" else "🎬"
                    caption += f"{sub_type} {sub.get('Name')} {sub_year}\n"
            
            base_url = cfg.get("emby_public_host") or host
            if base_url.endswith('/'): base_url = base_url[:-1]
            play_url = f"{base_url}/web/index.html#!/item?id={top.get('Id')}&serverId={top.get('ServerId')}"
            keyboard = {"inline_keyboard": [[{"text": "▶️ 立即播放", "url": play_url}]]}
            
            img_io = self._download_emby_image(top.get("Id"), 'Primary')
            if img_io: self.send_photo(chat_id, img_io, caption, reply_markup=keyboard)
            else: self.send_photo(chat_id, REPORT_COVER_URL, caption, reply_markup=keyboard)
            
        except Exception as e:
            logger.error(f"Search Error: {e}")
            self.send_message(chat_id, "❌ 搜索时发生错误")

    def _cmd_stats(self, chat_id, period='day'):
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
            else: user_str = "暂无数据"
            tops = query_db(f"SELECT ItemName, COUNT(*) as c FROM PlaybackActivity {where} GROUP BY ItemName ORDER BY c DESC LIMIT 10", params)
            top_content = ""
            if tops:
                for i, item in enumerate(tops):
                    prefix = ['🥇','🥈','🥉'][i] if i < 3 else f"{i+1}."
                    top_content += f"{prefix} {item['ItemName']} ({item['c']}次)\n"
            else: top_content = "暂无数据"
            
            yesterday_date = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%m-%d")
            title_display = f"{title_cn} ({yesterday_date})" if period == 'yesterday' else title_cn
            caption = (f"📊 <b>EmbyPulse {title_display}</b>\n───────────────\n📈 <b>数据大盘</b>\n▶️ 总播放量: {plays} 次\n⏱️ 活跃时长: {hours} 小时\n👥 活跃人数: {users} 人\n───────────────\n🏆 <b>活跃用户 Top 5</b>\n{user_str}───────────────\n🔥 <b>热门内容 Top 10</b>\n{top_content}")
            if HAS_PIL:
                img = report_gen.generate_report('all', period)
                if img: self.send_photo(chat_id, img, caption)
                else: self.send_message(chat_id, caption)
            else: self.send_photo(chat_id, REPORT_COVER_URL, caption)
        except Exception as e:
            logger.error(f"Stats Error: {e}")
            self.send_message(chat_id, f"❌ 统计失败: 数据库查询错误")

    def _daily_report_task(self):
        chat_id = str(cfg.get("tg_chat_id"))
        if not chat_id: return
        where = "WHERE DateCreated >= date('now', '-1 day', 'start of day') AND DateCreated < date('now', 'start of day')"
        res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}")
        count = res[0]['c'] if res else 0
        if count == 0:
            yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            msg = (f"📅 <b>昨日日报 ({yesterday_str})</b>\n------------------\n😴 昨天服务器静悄悄，大家都去现充了吗？\n\n📊 活跃用户: 0 人\n⏳ 播放时长: 0 分钟")
            self.send_message(chat_id, msg)
        else: self._cmd_stats(chat_id, 'yesterday')

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

    def _cmd_recent(self, cid):
        try:
            rows = query_db("SELECT UserId, ItemName, DateCreated FROM PlaybackActivity ORDER BY DateCreated DESC LIMIT 10")
            if not rows: return self.send_message(cid, "📭 无记录")
            msg = "📜 <b>最近播放</b>\n"
            for r in rows:
                date = r['DateCreated'][:16].replace('T', ' ')
                name = self._get_username(r['UserId'])
                msg += f"\n⏰ {date} | {name}\n🎬 {r['ItemName']}\n"
            self.send_message(cid, msg)
        except Exception as e: 
            logger.error(f"Recent Error: {e}")
            self.send_message(cid, f"❌ 查询失败")

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
        self.send_message(cid, "🤖 /search, /stats, /weekly, /monthly, /now, /latest, /recent, /check")

    def _scheduler_loop(self):
        while self.running:
            try:
                now = datetime.datetime.now()
                if now.minute != self.last_check_min:
                    self.last_check_min = now.minute
                    if now.hour == 9 and now.minute == 0:
                        self._check_user_expiration()
                        if cfg.get("tg_chat_id"): self._daily_report_task()
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
        if not cfg.get("tg_chat_id"): return False
        self._cmd_stats(str(cfg.get("tg_chat_id")), period)
        return True

bot = TelegramBot()