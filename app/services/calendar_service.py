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
        # 缓存结构改为字典: { offset: {'data': ..., 'time': timestamp} }
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
        # 基准日期 = 今天 + 偏移周数
        target_date = datetime.date.today() + datetime.timedelta(weeks=week_offset)
        # 计算该周的周一
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
        today_real = datetime.date.today()
        
        for i in range(7):
            items = sorted(week_data[i], key=lambda x: x['air_date'])
            final_days.append({
                "date": week_dates[i].strftime("%Y-%m-%d"),
                "weekday_cn": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][i],
                "is_today": week_dates[i] == today_real, # 只有真正的今天才高亮
                "items": items
            })
        
        # 获取 Emby 地址 (优先用 public_host)
        emby_url = cfg.get("emby_public_host") or cfg.get("emby_host") or ""
        if emby_url.endswith('/'): emby_url = emby_url[:-1]

        result = {
            "days": final_days, 
            "updated_at": datetime.datetime.now().strftime("%H:%M"),
            "emby_url": emby_url,
            "date_range": f"{start_of_week.strftime('%m/%d')} - {end_of_week.strftime('%m/%d')}" # 返回日期范围给前端显示
        }
        
        # 写入缓存 (按 offset 存储)
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
        if not tmdb_id: return None

        try:
            url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={api_key}&language=zh-CN"
            res = requests.get(url, timeout=5, proxies=proxies) 
            if res.status_code != 200: return None
            
            data = res.json()
            candidates = []
            if data.get("last_episode_to_air"): candidates.append(data["last_episode_to_air"])
            if data.get("next_episode_to_air"): candidates.append(data["next_episode_to_air"])
            
            # 🔥 增强逻辑：如果只有本季最后一集，也要检查一下
            # 有时候 TMDB 返回的 next_episode 是空的（因为还没定档），但 last_episode 可能是两周前的
            # 我们还需要一种机制去获取“这一季的所有集”，但这会增加 API 消耗。
            # 目前维持原逻辑，只看 last 和 next，这能覆盖 90% 的连载场景。

            target_ep = None
            for ep in candidates:
                air_date_str = ep.get("air_date")
                if not air_date_str: continue
                air_date = datetime.datetime.strptime(air_date_str, "%Y-%m-%d").date()
                if start_date <= air_date <= end_date:
                    target_ep = ep
                    break 
            
            if not target_ep: return None

            air_date = datetime.datetime.strptime(target_ep["air_date"], "%Y-%m-%d").date()
            season_num = target_ep.get("season_number")
            ep_num = target_ep.get("episode_number")
            
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
            return None

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