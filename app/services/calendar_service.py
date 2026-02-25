import requests
import datetime
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.core.config import cfg
from app.core.database import query_db

logger = logging.getLogger("uvicorn")

class CalendarService:
    def __init__(self):
        self._cache = {}
        self._cache_time = 0
        self._cache_lock = threading.Lock()
        self.CACHE_TTL = 3600  # 缓存 1 小时

    def get_weekly_calendar(self):
        """
        获取本周的剧集更新日历
        """
        # 1. 检查缓存
        now = time.time()
        with self._cache_lock:
            if self._cache and (now - self._cache_time < self.CACHE_TTL):
                return self._cache

        api_key = cfg.get("tmdb_api_key")
        if not api_key:
            return {"error": "未配置 TMDB API Key"}

        # 2. 获取本周时间范围 (周一到周日)
        today = datetime.date.today()
        start_of_week = today - datetime.timedelta(days=today.weekday())
        end_of_week = start_of_week + datetime.timedelta(days=6)
        
        # 3. 从 Emby 获取所有“连载中”的剧集
        continuing_series = self._get_emby_continuing_series()
        if not continuing_series:
            return {"days": []}

        # 4. 并发查询 TMDB (提速)
        # 用 Dict 存储每一天的剧集： {0: [], 1: [], ... 6: []} 0=周一
        week_data = {i: [] for i in range(7)}
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_series = {
                executor.submit(self._fetch_series_status, s, api_key, start_of_week, end_of_week): s 
                for s in continuing_series
            }
            
            for future in as_completed(future_to_series):
                result = future.result()
                if result:
                    # result 结构: {'day_index': 0~6, 'data': {...}}
                    idx = result['day_index']
                    if 0 <= idx <= 6:
                        week_data[idx].append(result['data'])

        # 5. 排序每一天的数据 (按时间)
        final_days = []
        # 生成前端友好的结构
        week_dates = [start_of_week + datetime.timedelta(days=i) for i in range(7)]
        
        for i in range(7):
            items = sorted(week_data[i], key=lambda x: x['air_date'])
            final_days.append({
                "date": week_dates[i].strftime("%Y-%m-%d"),
                "weekday_cn": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][i],
                "is_today": week_dates[i] == today,
                "items": items
            })

        result = {"days": final_days, "updated_at": datetime.datetime.now().strftime("%H:%M")}
        
        # 写入缓存
        with self._cache_lock:
            self._cache = result
            self._cache_time = now
            
        return result

    def _get_emby_continuing_series(self):
        """从 Emby 获取连载中的剧集"""
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        user_id = self._get_admin_id()
        if not key or not host or not user_id: return []

        url = f"{host}/emby/Users/{user_id}/Items"
        params = {
            "IncludeItemTypes": "Series",
            "Recursive": "true",
            "Fields": "ProviderIds,Status,AirDays", # 获取状态和TMDB ID
            "IsVirtual": "false",
            "api_key": key
        }
        
        try:
            res = requests.get(url, params=params, timeout=10)
            if res.status_code == 200:
                items = res.json().get("Items", [])
                # 过滤：必须有 TMDB ID 且状态是 Continuing
                return [i for i in items if i.get("Status") == "Continuing" and i.get("ProviderIds", {}).get("Tmdb")]
        except Exception as e:
            logger.error(f"Emby Series Fetch Error: {e}")
            return []
        return []

    def _fetch_series_status(self, series, api_key, start_date, end_date):
        """查询 TMDB 并比对本地库存"""
        tmdb_id = series.get("ProviderIds", {}).get("Tmdb")
        if not tmdb_id: return None

        try:
            # 查询 TMDB 剧集详情 (包含 next_episode_to_air)
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}&language=zh-CN"
            res = requests.get(url, timeout=5)
            if res.status_code != 200: return None
            
            data = res.json()
            
            # 我们关注两个字段：last_episode_to_air (刚播的) 和 next_episode_to_air (将播的)
            candidates = []
            if data.get("last_episode_to_air"): candidates.append(data["last_episode_to_air"])
            if data.get("next_episode_to_air"): candidates.append(data["next_episode_to_air"])

            target_ep = None
            
            # 筛选：也就是本周内播出的那一集
            for ep in candidates:
                air_date_str = ep.get("air_date")
                if not air_date_str: continue
                air_date = datetime.datetime.strptime(air_date_str, "%Y-%m-%d").date()
                
                if start_date <= air_date <= end_date:
                    target_ep = ep
                    break # 找到一个就行 (通常一周只播一集)
            
            if not target_ep: return None

            # 找到了本周播出的集！
            air_date = datetime.datetime.strptime(target_ep["air_date"], "%Y-%m-%d").date()
            season_num = target_ep.get("season_number")
            ep_num = target_ep.get("episode_number")
            
            # 🔥 核心逻辑：检查 Emby 里有没有这一集
            has_file = self._check_emby_has_episode(series["Id"], season_num, ep_num)
            
            # 计算状态
            status = "upcoming" # 默认：即将播出
            today = datetime.date.today()
            
            if has_file:
                status = "ready" # 🟢 已入库
            elif air_date < today:
                status = "missing" # 🔴 已播出但未入库
            elif air_date == today:
                status = "today" # 🔵 今天播出

            return {
                "day_index": (air_date - start_date).days, # 0=周一
                "data": {
                    "series_name": series.get("Name"),
                    "series_id": series.get("Id"),
                    "ep_name": target_ep.get("name"),
                    "season": season_num,
                    "episode": ep_num,
                    "air_date": target_ep.get("air_date"),
                    "poster_path": data.get("poster_path"), # TMDB 海报
                    "status": status,
                    "overview": target_ep.get("overview")
                }
            }

        except Exception as e:
            return None

    def _check_emby_has_episode(self, series_id, season, episode):
        """检查 Emby 库里是否存在某集"""
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        user_id = self._get_admin_id()
        if not key or not host or not user_id: return False
        
        url = f"{host}/emby/Users/{user_id}/Items"
        params = {
            "ParentId": series_id,
            "Recursive": "true",
            "IncludeItemTypes": "Episode",
            "ParentIndexNumber": season, # 季
            "IndexNumber": episode,      # 集
            "Limit": 1,
            "api_key": key
        }
        try:
            res = requests.get(url, params=params, timeout=3)
            if res.status_code == 200:
                return res.json().get("TotalRecordCount", 0) > 0
        except: pass
        return False

    def _get_admin_id(self):
        # 简单复用 bot 里的逻辑，或者直接从 DB 拿，这里为了独立性重写一个简单的
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        try:
            res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
            if res.status_code == 200:
                return res.json()[0]['Id'] # 简单取第一个用户
        except: pass
        return None

import time
calendar_service = CalendarService()