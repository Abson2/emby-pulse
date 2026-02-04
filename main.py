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

# ================= 配置区域 =================
PORT = 10307
# 数据库路径
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")
# Emby 地址
EMBY_HOST = os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/')
# Emby API Key
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "").strip()
# 默认图片
FALLBACK_IMAGE_URL = "https://img.hotimg.com/a444d32a033994d5b.png"

print(f"--- EmbyPulse V17 (Compatibility Fix) Starting ---")
print(f"DB Path: {DB_PATH}")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if not os.path.exists("static"): os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ================= 数据库工具 =================
def query_db(query, args=(), one=False):
    if not os.path.exists(DB_PATH): 
        print(f"❌ Error: DB file not found: {DB_PATH}")
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
        print(f"❌ SQL Error: {e} | Query: {query}")
        return None

def get_user_map():
    user_map = {}
    if EMBY_API_KEY:
        try:
            res = requests.get(f"{EMBY_HOST}/emby/Users?api_key={EMBY_API_KEY}", timeout=1)
            if res.status_code == 200:
                for u in res.json(): user_map[u['Id']] = u['Name']
        except: pass
    return user_map

# ================= 页面路由 =================
@app.get("/")
async def page_dashboard(request: Request): return templates.TemplateResponse("index.html", {"request": request, "active_page": "dashboard"})
@app.get("/content")
async def page_content(request: Request): return templates.TemplateResponse("content.html", {"request": request, "active_page": "content"})
@app.get("/report")
async def page_report(request: Request): return templates.TemplateResponse("report.html", {"request": request, "active_page": "report"})
@app.get("/details")
async def page_details(request: Request): return templates.TemplateResponse("details.html", {"request": request, "active_page": "details"})

# ================= API 接口 =================

@app.get("/api/users")
async def api_get_users():
    try:
        results = query_db("SELECT DISTINCT UserId FROM PlaybackActivity")
        if not results: return {"status": "success", "data": []}
        user_map = get_user_map()
        data = []
        for row in results:
            uid = row['UserId']
            if not uid: continue
            name = user_map.get(uid, f"User {str(uid)[:5]}")
            data.append({"UserId": uid, "UserName": name})
        data.sort(key=lambda x: x['UserName'])
        return {"status": "success", "data": data}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.get("/api/stats/dashboard")
async def api_dashboard(user_id: Optional[str] = None):
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
    except: return {"status": "error", "data": {"total_plays":0}}

# === 🔥 首页最近播放 (智能聚合修复版) ===
@app.get("/api/stats/recent")
async def api_recent_activity(user_id: Optional[str] = None):
    print(f"\n🔍 [Recent] Fetching for {user_id}")
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
            
        # 🔥 修正：移除了 SeriesName，防止报错
        sql = f"SELECT DateCreated, UserId, ItemId, ItemName, ItemType FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 200"
        results = query_db(sql, params)
        
        if not results: return {"status": "success", "data": []}
        
        user_map = get_user_map()
        final_data = []
        seen_keys = set() # 用于去重

        for row in results:
            item = dict(row)
            item['UserName'] = user_map.get(item['UserId'], "User")
            
            # 智能清洗名称
            raw_name = item['ItemName']
            clean_name = raw_name
            
            # 如果包含 " - S01" 或 " - 第" 等特征，截取前面部分
            if ' - ' in raw_name:
                parts = raw_name.split(' - ')
                clean_name = parts[0]
            
            item['DisplayName'] = clean_name
            
            # 聚合逻辑：如果是剧集，且已经出现过这个剧名，则跳过（只显示最近的一集）
            # 如果是电影，则不去重（可能重刷）
            if item['ItemType'] == 'Episode':
                if clean_name in seen_keys:
                    continue
                seen_keys.add(clean_name)
            
            final_data.append(item)
            if len(final_data) >= 20: break # 限制返回数量

        print(f"✅ [Recent] Aggregated {len(final_data)} items")
        return {"status": "success", "data": final_data}
    except Exception as e: 
        print(f"❌ [Recent] Error: {e}")
        return {"status": "error", "data": []}

@app.get("/api/live")
async def api_live_sessions():
    if not EMBY_API_KEY: return {"status": "error"}
    try:
        res = requests.get(f"{EMBY_HOST}/emby/Sessions?api_key={EMBY_API_KEY}", timeout=2)
        if res.status_code == 200:
            return {"status": "success", "data": [s for s in res.json() if s.get("NowPlayingItem")]}
    except: pass
    return {"status": "success", "data": []}

# === 🔥 海报数据接口 (智能聚合修复版) ===
@app.get("/api/stats/poster_data")
async def api_poster_data(user_id: Optional[str] = None, period: str = 'all'):
    print(f"\n📊 [Poster] User={user_id}, Period={period}")
    try:
        where, params = "WHERE 1=1", []
        date_filter = ""
        if period == 'week': date_filter = " AND DateCreated > date('now', '-7 days')"
        elif period == 'month': date_filter = " AND DateCreated > date('now', '-30 days')"
        elif period == 'year': date_filter = " AND DateCreated > date('now', '-1 year')"
        
        # 全服数据
        server_res = query_db(f"SELECT COUNT(*) as Plays FROM PlaybackActivity WHERE 1=1 {date_filter}")
        server_plays = server_res[0]['Plays'] if server_res else 0

        # 个人数据
        if user_id and user_id != 'all': 
            where += " AND UserId = ?"
            params.append(user_id)
        where += date_filter

        # 🔥 修正：移除了 SeriesName，防止报错
        raw_sql = f"SELECT ItemName, ItemId, ItemType, PlayDuration FROM PlaybackActivity {where}"
        rows = query_db(raw_sql, params)
        
        total_plays = 0
        total_duration = 0
        aggregated = {} 

        if rows:
            for row in rows:
                total_plays += 1
                dur = row['PlayDuration'] or 0
                total_duration += dur
                
                raw_name = row['ItemName']
                clean_name = raw_name
                
                # --- 核心聚合逻辑 ---
                # 针对 "剧名 - S01E01 - 标题" 这种格式进行清洗
                if ' - ' in raw_name:
                    # 尝试分割
                    parts = raw_name.split(' - ')
                    # 取第一部分作为剧名/片名
                    clean_name = parts[0]
                
                if clean_name not in aggregated:
                    aggregated[clean_name] = {'ItemName': clean_name, 'ItemId': row['ItemId'], 'Count': 0, 'Duration': 0}
                
                aggregated[clean_name]['Count'] += 1
                aggregated[clean_name]['Duration'] += dur
                aggregated[clean_name]['ItemId'] = row['ItemId'] 

        top_list = list(aggregated.values())
        top_list.sort(key=lambda x: x['Count'], reverse=True)
        top_list = top_list[:10]

        total_hours = round(total_duration / 3600)
        
        print(f"   ✅ Data Ready: Plays={total_plays}, TopList={len(top_list)}")

        return {
            "status": "success",
            "data": {
                "plays": total_plays,
                "hours": total_hours,
                "server_plays": server_plays,
                "top_list": top_list,
                "tags": ["观影达人"]
            }
        }
    except Exception as e:
        print(f"❌ [Poster] Error: {e}")
        return {"status": "error", "message": str(e), "data": {"plays": 0, "hours": 0, "server_plays": 0, "top_list": []}}

@app.get("/api/proxy/image/{item_id}/{img_type}")
async def proxy_image(item_id: str, img_type: str):
    target_id = item_id
    if img_type == 'primary' and EMBY_API_KEY:
        try:
            r = requests.get(f"{EMBY_HOST}/emby/Items?Ids={item_id}&Fields=SeriesId,ParentId&Limit=1&api_key={EMBY_API_KEY}", timeout=1)
            if r.status_code == 200:
                data = r.json()
                if data.get("Items"):
                    item = data["Items"][0]
                    if item.get('SeriesId'): target_id = item.get('SeriesId')
                    elif item.get('ParentId'): target_id = item.get('ParentId')
        except: pass

    suffix = "/Images/Backdrop?maxWidth=800" if img_type == 'backdrop' else "/Images/Primary?maxHeight=400"
    try:
        resp = requests.get(f"{EMBY_HOST}/emby/Items/{target_id}{suffix}", timeout=3)
        if resp.status_code == 200: return Response(content=resp.content, media_type="image/jpeg")
    except: pass
    return RedirectResponse(FALLBACK_IMAGE_URL)

# === 其他接口 ===
@app.get("/api/stats/chart")
async def api_chart_stats(user_id: Optional[str] = None, dimension: str = 'month'):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        sql = f"SELECT strftime('%Y-%m', DateCreated) as Label, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Label ORDER BY Label DESC LIMIT 6"
        results = query_db(sql, params)
        data = {}
        if results: 
            for r in results: data[r['Label']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

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
    return {"status": "success", "data": []}

@app.get("/api/stats/badges")
async def api_badges(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        badges = []
        night_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '02' AND '05'", params)
        if night_res and night_res[0]['c'] > 5:
            badges.append({"id": "night", "name": "修仙党", "icon": "fa-moon", "color": "text-purple-500", "bg": "bg-purple-100", "desc": "深夜是灵魂最自由的时刻"})
        return {"status": "success", "data": badges}
    except: return {"status": "success", "data": []}

@app.get("/api/stats/monthly_stats")
async def api_monthly_stats(user_id: Optional[str] = None):
    try:
        where, params = "WHERE DateCreated > date('now', '-12 months')", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        sql = f"SELECT strftime('%Y-%m', DateCreated) as Month, SUM(PlayDuration) as Duration FROM PlaybackActivity {where} GROUP BY Month ORDER BY Month"
        results = query_db(sql, params)
        data = {}
        if results:
            for r in results: data[r['Month']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)