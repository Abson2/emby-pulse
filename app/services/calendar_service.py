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
        self._cache = {}
        self._cache_time = 0
        self._cache_lock = threading.Lock()
        self.CACHE_TTL = 3600  # 缓存 1 小时

    def _get_proxies(self):
        """获取全局代理配置"""
        proxy = cfg.get("proxy_url")
        if proxy:
            return {"http": proxy, "https": proxy}
        return None

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

        # 2. 获取本周时间范围
        today = datetime.date.today()
        start_of_week = today - datetime.timedelta(days=today.weekday())
        end_of_week = start_of_week + datetime.timedelta(days=6)
        
        # 3. 从 Emby 获取所有“连载中”的剧集
        continuing_series = self._get_emby_continuing_series()
        if not continuing_series:
            return {"days": []}

        # 4. 并发查询 TMDB (带代理!)
        week_data = {i: [] for i in range(7)}
        proxies = self._get_proxies() # 获取代理
        
        # 增加线程数到 20 以加速 I/O
        with ThreadPoolExecutor(max_workers=20) as executor:
            future_to_series = {
                executor.submit(self._fetch_series_status, s, api_key, start_of_week, end_of_week, proxies): s 
                for s in continuing_series
            }
            
            for future in as_completed(future_to_series):
                try:
                    result = future.result()
                    if result:
                        idx = result['day_index']
                        if 0 <= idx <= 6:
                            week_data[idx].append(result['data'])
                except Exception as e:
                    logger.error(f"Calendar Task Error: {e}")

        # 5. 排序与格式化
        final_days = []
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
                # 过滤：必须有 TMDB ID 且状态是 Continuing
                return [i for i in items if i.get("Status") == "Continuing" and i.get("ProviderIds", {}).get("Tmdb")]
        except Exception as e:
            logger.error(f"Emby Series Fetch Error: {e}")
            return []
        return []

    def _fetch_series_status(self, series, api_key, start_date, end_date, proxies):
        """查询 TMDB 并比对本地库存"""
        tmdb_id = series.get("ProviderIds", {}).get("Tmdb")
        if not tmdb_id: return None

        try:
            # 🔥 修复：这里加上 proxies 参数
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}&language=zh-CN"
            res = requests.get(url, timeout=5, proxies=proxies) 
            
            if res.status_code != 200: return None
            
            data = res.json()
            candidates = []
            if data.get("last_episode_to_air"): candidates.append(data["last_episode_to_air"])
            if data.get("next_episode_to_air"): candidates.append(data["next_episode_to_air"])

            target_ep = None
            for ep in candidates:
                air_date_str = ep.get("air_date")
                if not air_date_str: continue
                # 简单解析 YYYY-MM-DD
                air_date = datetime.datetime.strptime(air_date_str, "%Y-%m-%d").date()
                
                if start_date <= air_date <= end_date:
                    target_ep = ep
                    break 
            
            if not target_ep: return None

            air_date = datetime.datetime.strptime(target_ep["air_date"], "%Y-%m-%d").date()
            season_num = target_ep.get("season_number")
            ep_num = target_ep.get("episode_number")
            
            # 检查 Emby 库存
            has_file = self._check_emby_has_episode(series["Id"], season_num, ep_num)
            
            status = "upcoming"
            today = datetime.date.today()
            
            if has_file:
                status = "ready"
            elif air_date < today:
                status = "missing"
            elif air_date == today:
                status = "today"

            return {
                "day_index": (air_date - start_date).days,
                "data": {
                    "series_name": series.get("Name"),
                    "series_id": series.get("Id"),
                    "ep_name": target_ep.get("name"),
                    "season": season_num,
                    "episode": ep_num,
                    "air_date": target_ep.get("air_date"),
                    "poster_path": data.get("poster_path"),
                    "status": status,
                    "overview": target_ep.get("overview")
                }
            }
        except Exception as e:
            # 某个剧查不到就算了，不要卡住
            return None

    def _check_emby_has_episode(self, series_id, season, episode):
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        user_id = self._get_admin_id()
        if not key or not host or not user_id: return False
        
        # 优化：只查Id，减少数据量
        url = f"{host}/emby/Users/{user_id}/Items"
        params = {
            "ParentId": series_id,
            "Recursive": "true",
            "IncludeItemTypes": "Episode",
            "ParentIndexNumber": season,
            "IndexNumber": episode,
            "Limit": 1,
            "Fields": "Id", # 只拿ID，快一点
            "api_key": key
        }
        try:
            res = requests.get(url, params=params, timeout=2) # 超时设短一点
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
                # 优先找管理员
                for u in users:
                    if u.get("Policy", {}).get("IsAdministrator"):
                        return u['Id']
                return users[0]['Id']
        except: pass
        return None

calendar_service = CalendarService()