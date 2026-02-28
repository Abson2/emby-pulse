import requests
import datetime
import logging
import threading
import time
import sqlite3
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.core.config import cfg
from app.core.database import DB_PATH

# 初始化日志记录器
logger = logging.getLogger("uvicorn")

class CalendarService:
    def __init__(self):
        # 内存缓存结构: { offset: {'data': ..., 'time': timestamp} }
        self._cache = {} 
        self._cache_lock = threading.Lock()
        
        # 🔥 启动后台守护线程：定时执行全量同步
        self._start_background_sync()

    def _start_background_sync(self):
        """
        后台独立线程：每隔 12 小时自动拉取 TMDB 排期并落盘。
        防止用户在服务器重启或长时间未访问后，首次打开页面加载过慢。
        """
        def sync_task():
            # 延迟 60 秒启动，确保系统核心组件（如数据库、网络代理）已就绪
            time.sleep(60)
            while True:
                try:
                    logger.info("🔄 [定时任务] 开始在后台自动刷新追剧日历缓存...")
                    # 强制同步本周 (0) 和 下周 (1) 的数据
                    self.get_weekly_calendar(force_refresh=True, week_offset=0)
                    self.get_weekly_calendar(force_refresh=True, week_offset=1)
                    logger.info("✅ [定时任务] 追剧日历后台更新成功，数据已持久化至 SQLite。")
                except Exception as e:
                    logger.error(f"❌ [定时任务] 后台同步日历失败: {e}")
                
                # 休眠 12 小时 (43200秒)
                time.sleep(43200)
        
        # daemon=True 确保主进程退出时线程能正常销毁
        t = threading.Thread(target=sync_task, daemon=True)
        t.start()

    def _get_proxies(self):
        """获取全局代理配置，用于 TMDB 请求"""
        proxy = cfg.get("proxy_url")
        if proxy:
            return {"http": proxy, "https": proxy}
        return None

    def mark_episode_ready(self, series_id, season, episode):
        """
        Webhook 联动接口：当 Emby 有新剧集入库时被调用。
        直接修改本地数据库状态，实现红灯变绿灯的实时感。
        """
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            # 根据系列ID、季、集 精准更新状态为 ready
            c.execute('''UPDATE tv_calendar_cache 
                         SET status = 'ready' 
                         WHERE series_id = ? AND season = ? AND episode = ?''', 
                      (series_id, season, episode))
            conn.commit()
            conn.close()
            
            # 清理内存缓存，确保下次刷新页面时读到最新状态
            with self._cache_lock:
                self._cache.clear()
            logger.info(f"🟢 [日历联动] Webhook 触发成功，已点亮绿灯: SeriesId={series_id} S{season}E{episode}")
        except Exception as e:
            logger.error(f"❌ 日历状态更新失败: {e}")

    def get_weekly_calendar(self, force_refresh=False, week_offset=0):
        """
        核心方法：获取周历数据
        逻辑流：内存缓存 -> 本地 SQLite 缓存 -> TMDB API (异步抓取)
        """
        now = time.time()
        # 缓存生存时间，默认 24 小时
        cache_ttl = int(cfg.get("calendar_cache_ttl") or 86400)

        # 1. 第一层防御：检查内存二级缓存
        if not force_refresh:
            with self._cache_lock:
                cached_item = self._cache.get(week_offset)
                if cached_item and (now - cached_item['time'] < cache_ttl):
                    return cached_item['data']

        api_key = cfg.get("tmdb_api_key")
        if not api_key:
            return {"error": "未配置 TMDB API Key，请在设置中配置"}

        # 2. 计算目标周的日期范围
        target_date = datetime.date.today() + datetime.timedelta(weeks=week_offset)
        start_of_week = target_date - datetime.timedelta(days=target_date.weekday())
        end_of_week = start_of_week + datetime.timedelta(days=6)
        
        # 3. 获取正在连载的剧集
        continuing_series = self._get_emby_continuing_series()
        if not continuing_series:
            return {"days": []}

        # 4. 第二层防御：从本地 SQLite 获取这一周的缓存数据
        week_data = {i: [] for i in range(7)}
        start_date_str = start_of_week.strftime("%Y-%m-%d")
        end_date_str = end_of_week.strftime("%Y-%m-%d")
        
        has_db_data = False
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT status, data_json FROM tv_calendar_cache WHERE air_date >= ? AND air_date <= ?", 
                      (start_date_str, end_date_str))
            rows = c.fetchall()
            
            # 只有在非强制刷新且本地有数据时，才直接使用 DB 数据
            if rows and not force_refresh:
                has_db_data = True
                for row in rows:
                    db_status = row[0]
                    item_data = json.loads(row[1])
                    # 用最新的 DB 状态（可能被 Webhook 修改过）覆盖 JSON 里的原始状态
                    item_data["status"] = db_status
                    
                    try:
                        air_date_obj = datetime.datetime.strptime(item_data["air_date"], "%Y-%m-%d").date()
                        day_index = (air_date_obj - start_of_week).days
                        if 0 <= day_index <= 6:
                            week_data[day_index].append(item_data)
                    except: continue
            conn.close()
        except Exception as e:
            logger.error(f"SQLite 读取异常: {e}")

        # 5. 第三层逻辑：如果本地无数据或强制刷新，执行异步抓取
        if not has_db_data or force_refresh:
            week_data = {i: [] for i in range(7)} # 重置结果集
            proxies = self._get_proxies()
            
            with ThreadPoolExecutor(max_workers=20) as executor:
                future_to_series = {
                    executor.submit(self._fetch_series_status, s, api_key, start_of_week, end_of_week, proxies): s 
                    for s in continuing_series
                }
                
                for future in as_completed(future_to_series):
                    try:
                        results = future.result()
                        if results:
                            for item in results:
                                idx = item['day_index']
                                if 0 <= idx <= 6:
                                    week_data[idx].append(item['data'])
                    except Exception as e:
                        logger.error(f"TMDB Fetcher Task Error: {e}")
            
            # 🔥 数据持久化：将新抓取的数据存入 SQLite
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                for i in range(7):
                    for data_dict in week_data[i]:
                        s_id = data_dict.get("series_id")
                        sn = data_dict.get("season")
                        en = data_dict.get("episode")
                        air_d = data_dict.get("air_date")
                        stat = data_dict.get("status")
                        
                        if s_id and sn is not None and en is not None:
                            id_key = f"{s_id}_{sn}_{en}"
                            c.execute('''INSERT OR REPLACE INTO tv_calendar_cache 
                                         (id, series_id, season, episode, air_date, status, data_json) 
                                         VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                                      (id_key, s_id, sn, en, air_d, stat, json.dumps(data_dict)))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"SQLite 写入异常: {e}")

        # 6. 智能去重与多集聚合逻辑 (例如 S01E01-E02)
        for i in range(7):
            raw_items = week_data[i]
            if not raw_items: continue

            grouped = {}
            for item in raw_items:
                key = (item.get('tmdb_id') or item['series_id'], item['season'])
                if key not in grouped:
                    grouped[key] = []
                grouped[key].append(item)
            
            merged_items = []
            for key, group in grouped.items():
                # 排序保证连号集数能正确展示
                sorted_eps = sorted(group, key=lambda x: x['episode'])
                if not sorted_eps: continue

                if len(sorted_eps) == 1:
                    merged_items.append(sorted_eps[0])
                else:
                    first, last = sorted_eps[0], sorted_eps[-1]
                    merged = first.copy()
                    merged['episode'] = f"{first['episode']}-{last['episode']}"
                    merged['ep_name'] = None 
                    # 只要有一集缺失，整体就标记为缺失
                    statuses = [x['status'] for x in sorted_eps]
                    if 'missing' in statuses: merged['status'] = 'missing'
                    elif 'ready' in statuses: merged['status'] = 'ready'
                    else: merged['status'] = 'upcoming'
                    merged_items.append(merged)
            
            # 按集数排序并更新结果集
            week_data[i] = sorted(merged_items, key=lambda x: str(x['episode']))

        # 7. 最终响应格式化
        final_days = []
        week_dates = [start_of_week + datetime.timedelta(days=i) for i in range(7)]
        today_real = datetime.date.today()
        
        for i in range(7):
            final_days.append({
                "date": week_dates[i].strftime("%Y-%m-%d"),
                "weekday_cn": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][i],
                "is_today": week_dates[i] == today_real, 
                "items": week_data[i]
            })
        
        # 获取 Emby 基本地址
        emby_url = (cfg.get("emby_public_url") or cfg.get("emby_host") or "").rstrip('/')

        # 动态获取当前 Emby 的 ServerId 用于前端跳转播放
        server_id = ""
        try:
            key, host = cfg.get("emby_api_key"), cfg.get("emby_host")
            sys_res = requests.get(f"{host}/emby/System/Info?api_key={key}", timeout=5)
            if sys_res.status_code == 200:
                server_id = sys_res.json().get("Id", "")
        except: pass

        result = {
            "days": final_days, 
            "emby_url": emby_url,
            "server_id": server_id,
            "date_range": f"{start_of_week.strftime('%m/%d')} - {end_of_week.strftime('%m/%d')}",
            "current_ttl": cache_ttl 
        }
        
        # 写入内存缓存
        with self._cache_lock:
            self._cache[week_offset] = {'data': result, 'time': now}
            
        return result

    def _get_emby_continuing_series(self):
        """从 Emby 获取所有状态为 Continuing 的剧集"""
        key, host = cfg.get("emby_api_key"), cfg.get("emby_host")
        user_id = self._get_admin_id()
        if not key or not host or not user_id: return []

        try:
            url = f"{host}/emby/Users/{user_id}/Items"
            params = {
                "IncludeItemTypes": "Series",
                "Recursive": "true",
                "Fields": "ProviderIds,Status",
                "IsVirtual": "false",
                "api_key": key
            }
            res = requests.get(url, params=params, timeout=10)
            if res.status_code == 200:
                items = res.json().get("Items", [])
                return [i for i in items if i.get("Status") == "Continuing" and i.get("ProviderIds", {}).get("Tmdb")]
        except Exception as e:
            logger.error(f"Emby API 请求失败: {e}")
            return []
        return []

    def _fetch_series_status(self, series, api_key, start_date, end_date, proxies):
        """抓取 TMDB 数据并对比本地库存"""
        tmdb_id = series.get("ProviderIds", {}).get("Tmdb")
        if not tmdb_id: return []

        try:
            # 1. 抓取剧集基本信息，提取剧集总简介 (series_overview) 用于前端兜底
            url_series = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}&language=zh-CN"
            res_series = requests.get(url_series, timeout=5, proxies=proxies)
            if res_series.status_code != 200: return []
            
            data_series = res_series.json()
            series_overview = data_series.get("overview") 
            
            # 2. 锁定目标季（抓取最后播出的和下次播出的季）
            target_seasons = set()
            if data_series.get("last_episode_to_air"):
                target_seasons.add(data_series["last_episode_to_air"].get("season_number"))
            if data_series.get("next_episode_to_air"):
                target_seasons.add(data_series["next_episode_to_air"].get("season_number"))
            if not target_seasons and data_series.get("seasons"):
                target_seasons.add(data_series["seasons"][-1].get("season_number"))

            final_episodes = []

            # 3. 遍历目标季，筛选出本周更新的单集
            for season_num in target_seasons:
                if season_num is None: continue
                url_season = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}?api_key={api_key}&language=zh-CN"
                res_season = requests.get(url_season, timeout=5, proxies=proxies)
                if res_season.status_code != 200: continue
                
                episodes_list = res_season.json().get("episodes", [])
                for ep in episodes_list:
                    air_date_str = ep.get("air_date")
                    if not air_date_str: continue
                    
                    try:
                        air_date = datetime.datetime.strptime(air_date_str, "%Y-%m-%d").date()
                        if start_date <= air_date <= end_date:
                            # 🔥 严格物理校验：去 Emby 匹配物理文件
                            has_file = self._check_emby_has_episode(series["Id"], ep["season_number"], ep["episode_number"])
                            
                            today = datetime.date.today()
                            status = "ready" if has_file else "missing" if air_date < today else "today" if air_date == today else "upcoming"

                            final_episodes.append({
                                "day_index": (air_date - start_date).days,
                                "data": {
                                    "series_name": series.get("Name"),
                                    "series_id": series.get("Id"),
                                    "tmdb_id": tmdb_id,
                                    "ep_name": ep.get("name"),
                                    "season": ep["season_number"],
                                    "episode": ep["episode_number"],
                                    "air_date": ep.get("air_date"),
                                    "poster_path": data_series.get("poster_path"),
                                    "status": status,
                                    "overview": ep.get("overview"),
                                    "series_overview": series_overview # 🔥 注入剧集总简介
                                }
                            })
                    except: continue
            return final_episodes
        except: return []

    def _check_emby_has_episode(self, series_id, season, episode):
        """
        [最严格物理校验]
        拉取该系列所有集数，手动核对季号、集号，并确保 Path 或 MediaSources 存在
        绕过 Emby API 无法按季集号过滤虚拟占位符的 Bug
        """
        key, host = cfg.get("emby_api_key"), cfg.get("emby_host")
        user_id = self._get_admin_id()
        if not key or not host or not user_id: return False
        
        try:
            url = f"{host}/emby/Users/{user_id}/Items"
            params = {
                "ParentId": series_id,
                "Recursive": "true",
                "IncludeItemTypes": "Episode",
                "Fields": "Path,MediaSources,LocationType", 
                "api_key": key
            }
            res = requests.get(url, params=params, timeout=5)
            if res.status_code == 200:
                items = res.json().get("Items", [])
                for item in items:
                    # 1. 核对季号和集号
                    if item.get("ParentIndexNumber") == season and item.get("IndexNumber") == episode:
                        # 2. 过滤虚拟和缺失标记
                        if item.get("LocationType", "") == "Virtual": continue
                        if item.get("IsMissing", False): continue
                        # 3. 物理路径校验：必须有文件路径或媒体流信息
                        if item.get("Path") or item.get("MediaSources"):
                            return True
        except: pass
        return False

    def _get_admin_id(self):
        """获取第一个管理员的 ID"""
        key, host = cfg.get("emby_api_key"), cfg.get("emby_host")
        try:
            res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=3)
            if res.status_code == 200:
                users = res.json()
                return next((u['Id'] for u in users if u.get("Policy", {}).get("IsAdministrator")), users[0]['Id'])
        except: pass
        return None

# 单例实例化
calendar_service = CalendarService()