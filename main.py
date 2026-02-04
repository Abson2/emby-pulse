import sqlite3
import os
import uvicorn
import requests
import datetime
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

# === 配置区域 ===
# 端口号
PORT = 10307
# 数据库路径 (请确保映射正确)
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")
# Emby 地址 (容器内部互联地址 或 局域网地址)
EMBY_HOST = os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/')
# Emby API Key (必须填写，否则实时监控和图片代理无法使用)
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "").strip()
# 默认图片 (当图片加载失败时显示)
FALLBACK_IMAGE_URL = "https://img.hotimg.com/a444d32a033994d5b.png"

print(f"--- EmbyPulse Ultimate V6 Starting ---")
print(f"DB Path: {DB_PATH}")
print(f"Emby API: {'✅ Loaded' if EMBY_API_KEY else '❌ Not Set (Live/Images disabled)'}")

app = FastAPI()

# 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件和模板
if not os.path.exists("static"): os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# === 数据库工具函数 ===
def query_db(query, args=(), one=False):
    """查询 SQLite 数据库"""
    if not os.path.exists(DB_PATH):
        print(f"Error: DB file not found at {DB_PATH}")
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, args)
        rv = cur.fetchall()
        conn.close()
        return (rv[0] if rv else None) if one else rv
    except Exception as e:
        print(f"DB Query Error: {e}")
        return None

def get_user_map():
    """获取用户 ID 到用户名的映射"""
    user_map = {}
    if EMBY_API_KEY:
        try:
            res = requests.get(f"{EMBY_HOST}/emby/Users?api_key={EMBY_API_KEY}", timeout=2)
            if res.status_code == 200:
                for u in res.json():
                    user_map[u['Id']] = u['Name']
        except:
            pass
    return user_map

# === 页面路由 ===
@app.get("/")
async def page_dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "active_page": "dashboard"})

@app.get("/content")
async def page_content(request: Request):
    return templates.TemplateResponse("content.html", {"request": request, "active_page": "content"})

@app.get("/report")
async def page_report(request: Request):
    return templates.TemplateResponse("report.html", {"request": request, "active_page": "report"})

@app.get("/details")
async def page_details(request: Request):
    return templates.TemplateResponse("details.html", {"request": request, "active_page": "details"})

# === API 接口 ===

@app.get("/api/users")
async def api_get_users():
    """获取有播放记录的用户列表"""
    try:
        results = query_db("SELECT DISTINCT UserId FROM PlaybackActivity")
        if not results: return {"status": "success", "data": []}
        
        user_map = get_user_map()
        data = []
        for row in results:
            uid = row['UserId']
            if not uid: continue
            # 优先用 API 获取的名字，没有则用 ID 截断
            name = user_map.get(uid, f"User {str(uid)[:5]}")
            data.append({"UserId": uid, "UserName": name})
        
        # 按名字排序
        data.sort(key=lambda x: x['UserName'])
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/stats/dashboard")
async def api_dashboard(user_id: Optional[str] = None):
    """仪表盘核心计数器"""
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
            
        plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where}", params)
        users = query_db(f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where} AND DateCreated > date('now', '-30 days')", params)
        dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
        
        return {"status": "success", "data": {
            "total_plays": plays[0]['c'] if plays else 0,
            "active_users": users[0]['c'] if users else 0,
            "total_duration": dur[0]['c'] if dur else 0
        }}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.get("/api/stats/recent")
async def api_recent_activity(user_id: Optional[str] = None):
    """最近播放记录 (返回50条供前端筛选)"""
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
            
        results = query_db(f"SELECT DateCreated, UserId, ItemId, ItemName, ItemType, PlayDuration FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 300", params)
        if not results: return {"status": "success", "data": []}

        raw_items = [dict(row) for row in results]
        user_map = get_user_map()
        metadata_map = {}
        
        # 批量获取元数据
        if EMBY_API_KEY:
            all_ids = [i['ItemId'] for i in raw_items][:100]
            # 分批查询避免 URL 过长
            chunk_size = 20
            for i in range(0, len(all_ids), chunk_size):
                try:
                    ids = ",".join(all_ids[i:i+chunk_size])
                    url = f"{EMBY_HOST}/emby/Items?Ids={ids}&Fields=SeriesId,SeriesName,ParentId&api_key={EMBY_API_KEY}"
                    res = requests.get(url, timeout=3)
                    if res.status_code == 200:
                        for m in res.json().get('Items', []):
                            metadata_map[m['Id']] = m
                except: pass

        final_data = []
        seen_keys = set()
        
        for item in raw_items:
            item['UserName'] = user_map.get(item['UserId'], "Unknown")
            
            # 智能聚合剧集
            display_id = item['ItemId']
            display_title = item['ItemName']
            unique_key = item['ItemName']
            meta = metadata_map.get(item['ItemId'])
            
            if meta:
                if meta.get('Type') == 'Episode':
                    if meta.get('SeriesId'):
                        display_id = meta.get('SeriesId')
                        unique_key = meta.get('SeriesId')
                        if meta.get('SeriesName'):
                            display_title = meta.get('SeriesName')
            elif ' - ' in item['ItemName']:
                 display_title = item['ItemName'].split(' - ')[0]
                 unique_key = display_title

            if unique_key not in seen_keys:
                seen_keys.add(unique_key)
                item['DisplayId'] = display_id
                item['DisplayTitle'] = display_title
                final_data.append(item)
            
            if len(final_data) >= 50: break 
            
        return {"status": "success", "data": final_data}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.get("/api/live")
async def api_live_sessions():
    """实时播放监控 (直接查询 Emby API)"""
    if not EMBY_API_KEY: return {"status": "error", "message": "No API Key"}
    try:
        url = f"{EMBY_HOST}/emby/Sessions?api_key={EMBY_API_KEY}"
        res = requests.get(url, timeout=3)
        if res.status_code != 200: return {"status": "error", "data": []}
        
        sessions = []
        for s in res.json():
            if s.get("NowPlayingItem"):
                info = {
                    "User": s.get("UserName", "Guest"),
                    "Client": s.get("Client", "Unknown"),
                    "Device": s.get("DeviceName", "Unknown"),
                    "ItemName": s["NowPlayingItem"].get("Name"),
                    "SeriesName": s["NowPlayingItem"].get("SeriesName", ""),
                    "ItemId": s["NowPlayingItem"].get("Id"),
                    "IsTranscoding": s.get("PlayState", {}).get("PlayMethod") == "Transcode",
                    "Percentage": int((s.get("PlayState", {}).get("PositionTicks", 0) / (s["NowPlayingItem"].get("RunTimeTicks", 1) or 1)) * 100)
                }
                sessions.append(info)
        return {"status": "success", "data": sessions}
    except Exception as e: return {"status": "error", "message": str(e)}

# === 🔥 核心升级: 映迹工坊数据接口 (V6) ===
@app.get("/api/stats/poster_data")
async def api_poster_data(user_id: Optional[str] = None, period: str = 'all'):
    """
    获取生成海报所需的所有数据
    period: 'all' | 'year' | 'month' | 'week'
    """
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        
        # 1. 构建时间过滤条件
        date_filter = ""
        if period == 'week':
            date_filter = " AND DateCreated > date('now', '-7 days')"
        elif period == 'month':
            date_filter = " AND DateCreated > date('now', '-30 days')"
        elif period == 'year':
            date_filter = " AND DateCreated > date('now', '-1 year')"
        
        where += date_filter
        
        # 2. 个人基础统计 (播放数, 时长)
        stats_sql = f"SELECT COUNT(*) as Plays, SUM(PlayDuration) as Duration FROM PlaybackActivity {where}"
        stats_res = query_db(stats_sql, params)
        total_plays = stats_res[0]['Plays'] if stats_res else 0
        total_hours = round((stats_res[0]['Duration'] or 0) / 3600)

        # 3. 🔥 全服数据统计 (用于对比)
        server_where = f"WHERE 1=1 {date_filter}" # 只应用时间过滤，不应用用户过滤
        server_sql = f"SELECT COUNT(*) as Plays FROM PlaybackActivity {server_where}"
        server_res = query_db(server_sql)
        server_total_plays = server_res[0]['Plays'] if server_res else 0

        # 4. 🔥 Top 10 内容榜单
        top_sql = f"""
        SELECT ItemName, ItemId, COUNT(*) as P, SUM(PlayDuration) as T 
        FROM PlaybackActivity {where} 
        GROUP BY ItemId, ItemName 
        ORDER BY P DESC LIMIT 10
        """
        top_res = query_db(top_sql, params)
        top_list = [dict(r) for r in top_res] if top_res else []

        # 5. 关键词生成逻辑
        tags = []
        if total_hours > 500: tags.append("影视肝帝")
        elif total_hours > 100: tags.append("忠实观众")
        
        # 深夜观看判断 (0点-5点)
        night_sql = f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '00' AND '05'"
        night_res = query_db(night_sql, params)
        if night_res and total_plays > 0 and (night_res[0]['c'] / total_plays > 0.2):
            tags.append("修仙党")
        
        if not tags: tags.append("佛系观众")

        return {
            "status": "success",
            "data": {
                "plays": total_plays,
                "hours": total_hours,
                "server_plays": server_total_plays, # 全服数据
                "top_list": top_list,               # Top 10 列表
                "tags": tags[:2],                   # 取前两个标签
                "active_hour": "--"                 # 暂留空
            }
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === 趋势图接口 ===
@app.get("/api/stats/chart")
async def api_chart_stats(user_id: Optional[str] = None, dimension: str = 'month'):
    """dimension: 'year', 'month', 'day'"""
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
            
        sql = ""
        if dimension == 'year':
            sql = f"SELECT strftime('%Y', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label DESC LIMIT 5"
        elif dimension == 'day':
            where += " AND DateCreated > date('now', '-30 days')"
            sql = f"SELECT date(DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
        else:
            where += " AND DateCreated > date('now', '-12 months')"
            sql = f"SELECT strftime('%Y-%m', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label"
            
        results = query_db(sql, params)
        data = {}
        if results:
            rows = results[::-1] if dimension == 'year' else results
            for r in rows:
                data[r['Label']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

# === 详情页接口 ===
@app.get("/api/stats/user_details")
async def api_user_details(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
            
        # 24小时分布
        hourly_res = query_db(f"SELECT strftime('%H', DateCreated) as Hour, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Hour ORDER BY Hour", params)
        hourly_data = {str(i).zfill(2): 0 for i in range(24)}
        if hourly_res:
            for r in hourly_res: hourly_data[r['Hour']] = r['Plays']
            
        # 设备分布
        device_res = query_db(f"SELECT COALESCE(DeviceName, ClientName, 'Unknown') as Device, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Device ORDER BY Plays DESC", params)
        
        # 日志
        logs_res = query_db(f"SELECT DateCreated, ItemName, PlayDuration, COALESCE(DeviceName, ClientName) as Device, UserId FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 100", params)
        
        user_map = get_user_map()
        logs_data = []
        if logs_res:
            for r in logs_res:
                l = dict(r)
                l['UserName'] = user_map.get(l['UserId'], "User")
                logs_data.append(l)
                
        return {"status": "success", "data": {
            "hourly": hourly_data, 
            "devices": [dict(r) for r in device_res] if device_res else [],
            "logs": logs_data
        }}
    except Exception as e: return {"status": "error", "message": str(e)}

# === 榜单接口 ===
@app.get("/api/stats/top_users_list")
async def api_top_users_list():
    try:
        res = query_db("SELECT UserId, COUNT(*) as Plays, SUM(PlayDuration) as TotalTime FROM PlaybackActivity GROUP BY UserId ORDER BY TotalTime DESC LIMIT 5")
        if not res: return {"status": "success", "data": []}
        user_map = get_user_map()
        data = []
        for row in res:
            u = dict(row)
            u['UserName'] = user_map.get(u['UserId'], f"User {str(u['UserId'])[:5]}")
            data.append(u)
        return {"status": "success", "data": data}
    except: return {"status": "success", "data": []}

@app.get("/api/stats/top_movies")
async def api_top_movies(user_id: Optional[str] = None, category: str = 'all', sort_by: str = 'count'):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        if category == 'Movie': where += " AND ItemType = 'Movie'"
        elif category == 'Episode': where += " AND ItemType = 'Episode'"
        
        order = "ORDER BY PlayCount DESC" if sort_by == 'count' else "ORDER BY TotalTime DESC"
        sql = f"SELECT ItemName, ItemId, ItemType, COUNT(*) as PlayCount, SUM(PlayDuration) as TotalTime FROM PlaybackActivity {where} GROUP BY ItemId, ItemName {order} LIMIT 20"
        
        results = query_db(sql, params)
        return {"status": "success", "data": [dict(r) for r in results] if results else []}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.get("/api/stats/badges")
async def api_badges(user_id: Optional[str] = None):
    """简单的勋章计算"""
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        badges = []
        # 这里保留之前的逻辑...
        night_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '02' AND '05'", params)
        if night_res and night_res[0]['c'] > 5:
            badges.append({"id": "night", "name": "修仙党", "icon": "fa-moon", "color": "text-purple-500", "bg": "bg-purple-100", "desc": "深夜是灵魂最自由的时刻"})
        
        dur_res = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
        if dur_res and dur_res[0]['c'] and dur_res[0]['c'] > 360000:
            badges.append({"id": "king", "name": "影视肝帝", "icon": "fa-crown", "color": "text-yellow-600", "bg": "bg-yellow-100", "desc": "阅片量惊人"})
            
        return {"status": "success", "data": badges}
    except: return {"status": "success", "data": []}

# === 图片代理 ===
@app.get("/api/proxy/image/{item_id}/{img_type}")
async def proxy_image(item_id: str, img_type: str):
    """代理 Emby 图片，解决跨域和内网访问问题"""
    target_id = item_id
    attempted_smart = False
    
    # 智能回退：如果是请求封面(primary)，尝试查找 SeriesId
    if img_type == 'primary' and EMBY_API_KEY:
        try:
            info_resp = requests.get(f"{EMBY_HOST}/emby/Items?Ids={item_id}&Fields=SeriesId,ParentId&Limit=1&api_key={EMBY_API_KEY}", timeout=2)
            if info_resp.status_code == 200:
                attempted_smart = True
                data = info_resp.json()
                if data.get("Items"):
                    item = data["Items"][0]
                    if item.get('Type') == 'Episode':
                        if item.get('SeriesId'): target_id = item.get('SeriesId')
                        elif item.get('ParentId'): target_id = item.get('ParentId')
        except: pass

    suffix = "/Images/Backdrop?maxWidth=800" if img_type == 'backdrop' else "/Images/Primary?maxHeight=400"
    
    try:
        # 尝试请求目标图片
        resp = requests.get(f"{EMBY_HOST}/emby/Items/{target_id}{suffix}", timeout=5)
        if resp.status_code == 200:
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
        
        # 如果失败且做过智能替换，尝试请求原始ID
        if attempted_smart and target_id != item_id:
            fallback_resp = requests.get(f"{EMBY_HOST}/emby/Items/{item_id}{suffix}", timeout=5)
            if fallback_resp.status_code == 200:
                return Response(content=fallback_resp.content, media_type=fallback_resp.headers.get("Content-Type", "image/jpeg"))
    except: pass
    
    # 最终回退
    return RedirectResponse(FALLBACK_IMAGE_URL)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)