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

logger = logging.getLogger("uvicorn")

class CalendarService:
    def __init__(self):
        # 缓存结构: { offset: {'data': ..., 'time': timestamp} }
        self._cache = {} 
        self._cache_lock = threading.Lock()
        
        # 🔥 启动后台定时同步任务
        self._start_background_sync()

    def _start_background_sync(self):
        """后台独立线程：定时拉取 TMDB 排期并落盘，防止用户首次打开加载过慢"""
        def sync_task():
            # 延迟 60 秒启动，等 FastAPI 主服务和数据库都彻底跑起来
            time.sleep(60)
            while True:
                try:
                    logger.info("🔄 [定时任务] 开始在后台自动拉取并更新追剧日历...")
                    # 强制刷新本周 (offset=0) 和 下周 (offset=1) 的数据写入本地 DB
                    self.get_weekly_calendar(force_refresh=True, week_offset=0)
                    self.get_weekly_calendar(force_refresh=True, week_offset=1)
                    logger.info("✅ [定时任务] 追剧日历更新完毕，数据已落盘。")
                except Exception as e:
                    logger.error(f"后台更新日历失败: {e}")
                
                # 休眠 12 小时 (43200秒) 后再次执行
                time.sleep(43200)
        
        # 设置 daemon=True，这样主进程结束时，这个线程也会自动销毁
        t = threading.Thread(target=sync_task, daemon=True)
        t.start()

    def _get_proxies(self):
        """获取全局代理配置"""
        proxy = cfg.get("proxy_url")
        if proxy:
            return {"http": proxy, "https": proxy}
        return None

    def mark_episode_ready(self, series_id, season, episode):
        """Webhook 专用：新集入库时，将本地缓存状态点亮为已入库 (ready)"""
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute('''UPDATE tv_calendar_cache 
                         SET status = 'ready' 
                         WHERE series_id = ? AND season = ? AND episode = ?''', 
                      (series_id, season, episode))
            conn.commit()
            conn.close()
            # 清理内存缓存，确保下次刷新页面时读到最新绿灯
            with self._cache_lock:
                self._cache.clear()
            logger.info(f"🟢 [日历联动] 剧集入库，红灯变绿灯: SeriesId={series_id} S{season}E{episode}")
        except Exception as e:
            logger.error(f"日历状态更新失败: {e}")

    def get_weekly_calendar(self, force_refresh=False, week_offset=0):
        """
        获取周历
        """
        now = time.time()
        
        # 动态获取配置，默认 1 天 (86400秒)
        cache_ttl = int(cfg.get("calendar_cache_ttl") or 86400)

        # 1. 检查对应周的内存缓存 (如果是前端普通请求，且没过期)
        if not force_refresh:
            with self._cache_lock:
                cached_item = self._cache.get(week_offset)
                if cached_item and (now - cached_item['time'] < cache_ttl):
                    return cached_item['data']

        api_key = cfg.get("tmdb_api_key")
        if not api_key:
            return {"error": "未配置 TMDB API Key"}

        # 2. 计算目标周的时间范围
        target_date = datetime.date.today() + datetime.timedelta(weeks=week_offset)
        start_of_week = target_date - datetime.timedelta(days=target_date.weekday())
        end_of_week = start_of_week + datetime.timedelta(days=6)
        
        # 3. 从 Emby 获取所有“连载中”的剧集
        continuing_series = self._get_emby_continuing_series()
        if not continuing_series:
            return {"days": []}

        # 4. 优化：先尝试从本地 SQLite 获取这一周的数据
        week_data = {i: [] for i in range(7)}
        start_date_str = start_of_week.strftime("%Y-%m-%d")
        end_date_str = end_of_week.strftime("%Y-%m-%d")
        
        has_db_data = False
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT status, data_json FROM tv_calendar_cache WHERE air_date >= ? AND air_date <= ?", (start_date_str, end_date_str))
            rows = c.fetchall()
            if rows and not force_refresh:
                has_db_data = True
                for row in rows:
                    db_status = row[0]
                    data_dict = json.loads(row[1])
                    data_dict["status"] = db_status # 🔥 关键：用 Webhook 更新后的最新状态覆盖
                    
                    try:
                        air_date_obj = datetime.datetime.strptime(data_dict["air_date"], "%Y-%m-%d").date()
                        day_index = (air_date_obj - start_of_week).days
                        if 0 <= day_index <= 6:
                            week_data[day_index].append(data_dict)
                    except: pass
            conn.close()
        except Exception as e:
            logger.error(f"DB Read Error: {e}")

        # 5. 如果本地没数据或强制刷新，才去并发查 TMDB 和 Emby
        if not has_db_data or force_refresh:
            # 清空刚才可能加载的不完整本地数据
            week_data = {i: [] for i in range(7)}
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
                        logger.error(f"Calendar Task Error: {e}")
            
            # 🔥 新增：将查回来的全新数据落盘到本地数据库
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                for i in range(7):
                    for data_dict in week_data[i]:
                        series_id = data_dict.get("series_id")
                        season = data_dict.get("season")
                        episode = data_dict.get("episode")
                        air_date = data_dict.get("air_date")
                        status = data_dict.get("status")
                        
                        if series_id and season is not None and episode is not None:
                            id_key = f"{series_id}_{season}_{episode}"
                            c.execute('''INSERT OR REPLACE INTO tv_calendar_cache 
                                         (id, series_id, season, episode, air_date, status, data_json) 
                                         VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                                      (id_key, series_id, season, episode, air_date, status, json.dumps(data_dict)))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"DB Write Error: {e}")

        # 6. 智能合并与去重
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
                unique_eps = {}
                for x in group:
                    unique_eps[x['episode']] = x
                
                sorted_eps = sorted(unique_eps.values(), key=lambda x: x['episode'])
                if not sorted_eps: continue

                if len(sorted_eps) == 1:
                    merged_items.append(sorted_eps[0])
                else:
                    first = sorted_eps[0]
                    last = sorted_eps[-1]
                    merged = first.copy()
                    merged['episode'] = f"{first['episode']}-{last['episode']}"
                    merged['ep_name'] = None 
                    statuses = [x['status'] for x in sorted_eps]
                    if 'missing' in statuses:
                        merged['status'] = 'missing'
                    elif 'ready' in statuses:
                        merged['status'] = 'ready'
                    else:
                        merged['status'] = 'upcoming'
                    merged_items.append(merged)
            
            week_data[i] = merged_items

        # 7. 排序与格式化
        final_days = []
        week_dates = [start_of_week + datetime.timedelta(days=i) for i in range(7)]
        today_real = datetime.date.today()
        
        for i in range(7):
            items = sorted(week_data[i], key=lambda x: x['air_date'])
            final_days.append({
                "date": week_dates[i].strftime("%Y-%m-%d"),
                "weekday_cn": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][i],
                "is_today": week_dates[i] == today_real, 
                "items": items
            })
        
        # 获取公网/内网地址
        emby_url = cfg.get("emby_public_url") or cfg.get("emby_public_host") or cfg.get("emby_host") or ""
        if emby_url.endswith('/'): emby_url = emby_url[:-1]

        # 🔥 获取 Emby ServerId (解决跳转播放验证问题)
        server_id = ""
        try:
            key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
            sys_res = requests.get(f"{host}/emby/System/Info?api_key={key}", timeout=5)
            if sys_res.status_code == 200:
                server_id = sys_res.json().get("Id", "")
        except: pass

        result = {
            "days": final_days, 
            "updated_at": datetime.datetime.now().strftime("%H:%M"),
            "emby_url": emby_url,
            "server_id": server_id, # 🔥 返回 ServerId
            "date_range": f"{start_of_week.strftime('%m/%d')} - {end_of_week.strftime('%m/%d')}",
            "current_ttl": cache_ttl 
        }
        
        with self._cache_lock:
            self._cache[week_offset] = {
                'data': result,
                'time': now
            }
            
        return result

    def _get_emby_continuing_series(self):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        user_id = self._get_admin_id()
        if not key or not host or not user_id: return []

        url = f"{host}/emby/Users/{user_id}/Items"
        params = {
            "IncludeItemTypes": "Series",
            "Recursive": "true",
            "Fields": "ProviderIds,Status,AirDays",
            "IsVirtual": "false",
            "api_key": key
        }
        try:
            res = requests.get(url, params=params, timeout=10)
            if res.status_code == 200:
                items = res.json().get("Items", [])
                return [i for i in items if i.get("Status") == "Continuing" and i.get("ProviderIds", {}).get("Tmdb")]
        except Exception as e:
            logger.error(f"Emby Series Fetch Error: {e}")
            return []
        return []

    def _fetch_series_status(self, series, api_key, start_date, end_date, proxies):
        """查询 TMDB 并比对本地库存"""
        tmdb_id = series.get("ProviderIds", {}).get("Tmdb")
        if not tmdb_id: return []

        try:
            url_series = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}&language=zh-CN"
            res_series = requests.get(url_series, timeout=5, proxies=proxies)
            if res_series.status_code != 200: return []
            
            data_series = res_series.json()
            target_seasons = set()
            
            if data_series.get("last_episode_to_air"):
                target_seasons.add(data_series["last_episode_to_air"].get("season_number"))
            if data_series.get("next_episode_to_air"):
                target_seasons.add(data_series["next_episode_to_air"].get("season_number"))
            
            if not target_seasons and data_series.get("seasons"):
                last_season = data_series["seasons"][-1]
                target_seasons.add(last_season.get("season_number"))

            final_episodes = []

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
                    except: continue

                    if start_date <= air_date <= end_date:
                        season_val = ep.get("season_number")
                        ep_val = ep.get("episode_number")
                        
                        has_file = self._check_emby_has_episode(series["Id"], season_val, ep_val)
                        
                        status = "upcoming"
                        today = datetime.date.today()
                        
                        if has_file:
                            status = "ready"
                        elif air_date < today:
                            status = "missing"
                        elif air_date == today:
                            status = "today" 

                        final_episodes.append({
                            "day_index": (air_date - start_date).days,
                            "data": {
                                "series_name": series.get("Name"),
                                "series_id": series.get("Id"),
                                "tmdb_id": tmdb_id,
                                "ep_name": ep.get("name"),
                                "season": season_val,
                                "episode": ep_val,
                                "air_date": ep.get("air_date"),
                                "poster_path": data_series.get("poster_path"),
                                "status": status,
                                "overview": ep.get("overview")
                            }
                        })
            
            return final_episodes
        except Exception as e:
            return []

    def _check_emby_has_episode(self, series_id, season, episode):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        user_id = self._get_admin_id()
        if not key or not host or not user_id: return False
        
        url = f"{host}/emby/Users/{user_id}/Items"
        params = {
            "ParentId": series_id,
            "Recursive": "true",
            "IncludeItemTypes": "Episode",
            "ParentIndexNumber": season,
            "IndexNumber": episode,
            "IsVirtual": "false",        # 🔥 核心修复 1：直接在 API 层面拒收虚拟占位符
            "Limit": 1,
            "Fields": "Id,LocationType", # 🔥 请求返回 LocationType 字段
            "api_key": key
        }
        try:
            res = requests.get(url, params=params, timeout=2)
            if res.status_code == 200:
                items = res.json().get("Items", [])
                if items:
                    # 🔥 核心修复 2：双重保险，确保它是一个真实的物理文件而不是刮削的空壳
                    return items[0].get("LocationType", "") != "Virtual"
        except: pass
        return False

    def _get_admin_id(self):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=3)
            if res.status_code == 200:
                users = res.json()
                for u in users:
                    if u.get("Policy", {}).get("IsAdministrator"):
                        return u['Id']
                return users[0]['Id']
        except: pass
        return None

calendar_service = CalendarService()