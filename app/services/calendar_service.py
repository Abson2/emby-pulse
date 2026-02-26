import requests
import datetime
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.core.config import cfg

logger = logging.getLogger("uvicorn")

class CalendarService:
    def __init__(self):
        # 缓存结构: { offset: {'data': ..., 'time': timestamp} }
        self._cache = {} 
        self._cache_lock = threading.Lock()
        self.CACHE_TTL = 3600  # 缓存 1 小时

    def _get_proxies(self):
        """获取全局代理配置"""
        proxy = cfg.get("proxy_url")
        if proxy:
            return {"http": proxy, "https": proxy}
        return None

    def get_weekly_calendar(self, force_refresh=False, week_offset=0):
        """
        获取周历
        :param force_refresh: 强制刷新
        :param week_offset: 周偏移量 (0=本周, 1=下周, -1=上周)
        """
        now = time.time()
        
        # 1. 检查对应周的缓存
        if not force_refresh:
            with self._cache_lock:
                cached_item = self._cache.get(week_offset)
                if cached_item and (now - cached_item['time'] < self.CACHE_TTL):
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

        # 4. 并发查询 TMDB
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

        # 🔥 新增步骤：合并同一天的同一部剧 (Episode 13, 14 -> 13-14)
        for i in range(7):
            raw_items = week_data[i]
            if not raw_items: continue

            # 按 series_id 分组
            grouped = {}
            for item in raw_items:
                sid = item['series_id']
                if sid not in grouped:
                    grouped[sid] = []
                grouped[sid].append(item)
            
            # 执行合并
            merged_items = []
            for sid, group in grouped.items():
                if len(group) == 1:
                    merged_items.append(group[0])
                else:
                    # 排序，确保是 13, 14 而不是 14, 13
                    group.sort(key=lambda x: x['episode'])
                    first = group[0]
                    last = group[-1]
                    
                    # 复制一份作为合并后的对象
                    merged = first.copy()
                    
                    # 1. 合并集数: "13-14"
                    merged['episode'] = f"{first['episode']}-{last['episode']}"
                    
                    # 2. 合并单集标题 (可选，太长就省略)
                    # merged['ep_name'] = f"{first['ep_name']} ..." 

                    # 3. 合并状态 (优先级: 缺失 > 已入库 > 待播)
                    # 逻辑: 只要有一集缺失，整体就算缺失(提醒去补); 否则只要有一集入了，就算入了
                    statuses = [x['status'] for x in group]
                    if 'missing' in statuses:
                        merged['status'] = 'missing'
                    elif 'ready' in statuses:
                        merged['status'] = 'ready'
                    else:
                        merged['status'] = 'upcoming'
                    
                    merged_items.append(merged)
            
            # 将合并后的列表放回
            week_data[i] = merged_items

        # 5. 排序与格式化
        final_days = []
        week_dates = [start_of_week + datetime.timedelta(days=i) for i in range(7)]
        today_real = datetime.date.today()
        
        for i in range(7):
            # 按开播时间排序
            items = sorted(week_data[i], key=lambda x: x['air_date'])
            final_days.append({
                "date": week_dates[i].strftime("%Y-%m-%d"),
                "weekday_cn": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][i],
                "is_today": week_dates[i] == today_real, 
                "items": items
            })
        
        emby_url = cfg.get("emby_public_host") or cfg.get("emby_host") or ""
        if emby_url.endswith('/'): emby_url = emby_url[:-1]

        result = {
            "days": final_days, 
            "updated_at": datetime.datetime.now().strftime("%H:%M"),
            "emby_url": emby_url,
            "date_range": f"{start_of_week.strftime('%m/%d')} - {end_of_week.strftime('%m/%d')}"
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
                                "ep_name": ep.get("name"),
                                "season": season_val,
                                "episode": ep_val, # 这里还是数字，后面聚合时会变成字符串 "13-14"
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
            "Limit": 1,
            "Fields": "Id", 
            "api_key": key
        }
        try:
            res = requests.get(url, params=params, timeout=2)
            if res.status_code == 200:
                return res.json().get("TotalRecordCount", 0) > 0
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