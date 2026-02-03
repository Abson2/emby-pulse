import sqlite3
import os
import uvicorn
import requests
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

# === 配置 ===
PORT = 10307
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")
EMBY_HOST = os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/')
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "")

print(f"--- EmbyPulse 启动 ---")
print(f"DB: {DB_PATH}")
print(f"API: {'✅ 已加载' if EMBY_API_KEY else '❌ 未加载'}")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

def query_db(query, args=(), one=False):
    if not os.path.exists(DB_PATH): return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, args)
        rv = cur.fetchall()
        conn.close()
        return (rv[0] if rv else None) if one else rv
    except Exception as e:
        print(f"SQL Error: {e}")
        return None

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

# === API: 用户列表 (API 补全版) ===
@app.get("/api/users")
async def api_get_users():
    try:
        # 1. 从数据库只查 UserId (避开 UserName 不存在的问题)
        sql = "SELECT DISTINCT UserId FROM PlaybackActivity"
        results = query_db(sql)
        
        if not results:
            return {"status": "success", "data": []}

        # 2. 从 Emby API 获取所有真实用户，建立 ID->Name 映射
        user_map = {}
        if EMBY_API_KEY:
            try:
                res = requests.get(f"{EMBY_HOST}/emby/Users?api_key={EMBY_API_KEY}", timeout=3)
                if res.status_code == 200:
                    for u in res.json():
                        user_map[u['Id']] = u['Name']
            except Exception as e:
                print(f"Emby User API Failed: {e}")

        # 3. 组装数据
        data = []
        for row in results:
            uid = row['UserId']
            if not uid: continue
            
            # 如果 API 查到了名字就用 API 的，否则用 ID 截取
            name = user_map.get(uid, f"User {str(uid)[:5]}")
            data.append({"UserId": uid, "UserName": name})

        # 按名字排序
        data.sort(key=lambda x: x['UserName'])
        return {"status": "success", "data": data}
        
    except Exception as e:
        print(f"API Error: {e}")
        return {"status": "error", "message": str(e)}

# === API: 仪表盘 ===
@app.get("/api/stats/dashboard")
async def api_dashboard(user_id: Optional[str] = None):
    try:
        where_clause = "WHERE 1=1"
        params = []
        if user_id and user_id != 'all':
            where_clause += " AND UserId = ?"
            params.append(user_id)

        res_plays = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where_clause}", params)
        total_plays = res_plays[0]['c'] if res_plays else 0
        
        active_sql = f"SELECT COUNT(DISTINCT UserId) as c FROM PlaybackActivity {where_clause} AND DateCreated > date('now', '-30 days')"
        res_users = query_db(active_sql, params)
        active_users = res_users[0]['c'] if res_users else 0
        
        res_dur = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where_clause}", params)
        total_duration = res_dur[0]['c'] if res_dur and res_dur[0]['c'] else 0

        return {"status": "success", "data": {"total_plays": total_plays, "active_users": active_users, "total_duration": total_duration}}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === API: 热门内容 ===
@app.get("/api/stats/top_movies")
async def api_top_movies(user_id: Optional[str] = None):
    where_clause = ""
    params = []
    if user_id and user_id != 'all':
        where_clause = "WHERE UserId = ?"
        params.append(user_id)

    sql = f"""
    SELECT ItemName, ItemId, ItemType, COUNT(*) as PlayCount, SUM(PlayDuration) as TotalTime
    FROM PlaybackActivity
    {where_clause}
    GROUP BY ItemId, ItemName
    ORDER BY PlayCount DESC
    LIMIT 10
    """
    try:
        results = query_db(sql, params)
        data = [dict(row) for row in results] if results else []
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === API: 图片代理 ===
@app.get("/api/proxy/image/{item_id}/{img_type}")
async def proxy_image(item_id: str, img_type: str):
    target_id = item_id
    if img_type == 'primary' and EMBY_API_KEY:
        try:
            info_url = f"{EMBY_HOST}/emby/Items?Ids={item_id}&Fields=SeriesId,ParentId&Limit=1&api_key={EMBY_API_KEY}"
            info_resp = requests.get(info_url, timeout=3)
            if info_resp.status_code == 200:
                data = info_resp.json()
                if data.get("Items"):
                    item = data["Items"][0]
                    if item.get('Type') == 'Episode':
                        if item.get('SeriesId'): target_id = item.get('SeriesId')
                        elif item.get('ParentId'): target_id = item.get('ParentId')
        except: pass

    suffix = "/Images/Backdrop?maxWidth=800" if img_type == 'backdrop' else "/Images/Primary?maxHeight=400"
    try:
        resp = requests.get(f"{EMBY_HOST}/emby/Items/{target_id}{suffix}", timeout=5)
        return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
    except:
        return Response(status_code=404)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
