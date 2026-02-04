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
# 端口
PORT = 10307
# 数据库路径 (确保映射正确)
DB_PATH = os.getenv("DB_PATH", "/emby-data/playback_reporting.db")
# Emby 地址
EMBY_HOST = os.getenv("EMBY_HOST", "http://127.0.0.1:8096").rstrip('/')
# Emby API Key
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "").strip()
# 默认图片
FALLBACK_IMAGE_URL = "https://img.hotimg.com/a444d32a033994d5b.png"

print(f"--- EmbyPulse V11 (Backend Final) ---")
print(f"DB Path: {DB_PATH}")
print(f"API Status: {'✅ Ready' if EMBY_API_KEY else '⚠️ No API Key (Images/Live disabled)'}")

app = FastAPI()

# 跨域设置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件
if not os.path.exists("static"): os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ================= 数据库工具 =================
def query_db(query, args=(), one=False):
    """执行 SQL 查询，带错误处理"""
    if not os.path.exists(DB_PATH):
        print(f"❌ Error: Database file not found at {DB_PATH}")
        return None
    try:
        # 使用只读模式，避免锁库
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, args)
        rv = cur.fetchall()
        conn.close()
        return (rv[0] if rv else None) if one else rv
    except Exception as e:
        print(f"❌ DB Query Error: {e}")
        return None

def get_user_map():
    """获取用户 ID -> 用户名 映射"""
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

# ================= 页面路由 =================
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

# ================= 核心 API =================

@app.get("/api/users")
async def api_get_users():
    """获取用户列表"""
    try:
        # 只查询有播放记录的用户
        results = query_db("SELECT DISTINCT UserId FROM PlaybackActivity")
        if not results: return {"status": "success", "data": []}
        
        user_map = get_user_map()
        data = []
        for row in results:
            uid = row['UserId']
            if not uid: continue
            # 如果 API 没取到名字，就用 ID 前几位代替
            name = user_map.get(uid, f"User {str(uid)[:5]}")
            data.append({"UserId": uid, "UserName": name})
        
        # 按名字排序
        data.sort(key=lambda x: x['UserName'])
        return {"status": "success", "data": data}
    except Exception as e: 
        return {"status": "error", "message": str(e), "data": []}

@app.get("/api/stats/dashboard")
async def api_dashboard(user_id: Optional[str] = None):
    """仪表盘统计"""
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
    except: return {"status": "error", "data": {"total_plays":0, "active_users":0, "total_duration":0}}

@app.get("/api/stats/recent")
async def api_recent_activity(user_id: Optional[str] = None):
    """最近播放"""
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
            
        results = query_db(f"SELECT DateCreated, UserId, ItemId, ItemName, ItemType FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 100", params)
        if not results: return {"status": "success", "data": []}

        user_map = get_user_map()
        data = []
        for row in results:
            item = dict(row)
            item['UserName'] = user_map.get(item['UserId'], "User")
            data.append(item)
        return {"status": "success", "data": data[:20]}
    except: return {"status": "error", "data": []}

@app.get("/api/live")
async def api_live_sessions():
    """实时监控"""
    if not EMBY_API_KEY: return {"status": "error", "message": "No API Key"}
    try:
        res = requests.get(f"{EMBY_HOST}/emby/Sessions?api_key={EMBY_API_KEY}", timeout=2)
        if res.status_code == 200:
            sessions = []
            for s in res.json():
                if s.get("NowPlayingItem"):
                    sessions.append(s)
            return {"status": "success", "data": sessions}
    except: pass
    return {"status": "success", "data": []}

# === 🔥 映迹工坊：核心数据接口 (关键) ===
@app.get("/api/stats/poster_data")
async def api_poster_data(user_id: Optional[str] = None, period: str = 'all'):
    """
    海报数据源
    user_id: 指定用户
    period: 'week', 'month', 'year', 'all'
    """
    try:
        # 1. 构建时间过滤条件 (SQL片段)
        date_filter = ""
        if period == 'week': date_filter = " AND DateCreated > date('now', '-7 days')"
        elif period == 'month': date_filter = " AND DateCreated > date('now', '-30 days')"
        elif period == 'year': date_filter = " AND DateCreated > date('now', '-1 year')"
        
        # 2. 获取全服总数据 (不受用户ID限制，只受时间限制)
        server_sql = f"SELECT COUNT(*) as Plays FROM PlaybackActivity WHERE 1=1 {date_filter}"
        server_res = query_db(server_sql)
        server_plays = server_res[0]['Plays'] if server_res else 0

        # 3. 准备用户数据查询
        where = "WHERE 1=1" + date_filter
        params = []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)

        # 4. 拉取原始记录 (Raw Data)
        # 获取 ItemName, SeriesName 等用于聚合
        raw_sql = f"SELECT ItemName, ItemId, ItemType, SeriesName, PlayDuration FROM PlaybackActivity {where}"
        rows = query_db(raw_sql, params)
        
        # 初始化统计变量
        total_plays = 0
        total_duration = 0
        aggregated = {} 

        if rows:
            for row in rows:
                total_plays += 1
                dur = row['PlayDuration'] or 0
                total_duration += dur
                
                # --- 智能聚合逻辑 ---
                # 如果是剧集 (Episode) 且有 SeriesName，则按剧名聚合
                # 否则按 ItemName 聚合 (电影)
                item_name = row['SeriesName'] if (row['ItemType'] == 'Episode' and row['SeriesName']) else row['ItemName']
                
                # 清洗数据：移除 " - 1080p", " - 4K" 等后缀
                if item_name and ' - ' in item_name:
                    item_name = item_name.split(' - ')[0]
                
                if not item_name: item_name = "未知内容"

                if item_name not in aggregated:
                    aggregated[item_name] = {
                        'ItemName': item_name,
                        'ItemId': row['ItemId'], # 暂存 ID 用于获取图片
                        'Count': 0,
                        'Duration': 0
                    }
                
                aggregated[item_name]['Count'] += 1
                aggregated[item_name]['Duration'] += dur
                # 更新 ID 为最新的一条，确保获取到的封面是有效的
                aggregated[item_name]['ItemId'] = row['ItemId']

        # 5. 排序生成 Top 10
        top_list = list(aggregated.values())
        # 优先按播放次数降序，次数相同按时长降序
        top_list.sort(key=lambda x: (x['Count'], x['Duration']), reverse=True)
        top_list = top_list[:10] # 只取前10

        # 6. 计算总时长 (小时)
        total_hours = round(total_duration / 3600)

        # 7. 生成标签 (趣味性)
        tags = ["新晋观众"]
        if total_hours > 50: tags = ["忠实观众"]
        if total_hours > 200: tags = ["影视肝帝"]
        if total_plays > 500: tags.append("阅片无数")

        # 8. 返回最终 JSON
        return {
            "status": "success",
            "data": {
                "plays": total_plays,
                "hours": total_hours,
                "server_plays": server_plays, # 全服数据
                "top_list": top_list,         # 聚合后的 Top10
                "tags": tags[:2]              # 只取前两个标签
            }
        }

    except Exception as e:
        print(f"❌ Poster Data Error: {e}")
        # 发生错误时返回空结构，防止前端崩溃
        return {
            "status": "error",
            "message": str(e),
            "data": {
                "plays": 0, "hours": 0, "server_plays": 0, "top_list": [], "tags": ["数据异常"]
            }
        }

# === 图片代理 (解决跨域/内网问题) ===
@app.get("/api/proxy/image/{item_id}/{img_type}")
async def proxy_image(item_id: str, img_type: str):
    """
    img_type: 'primary' (封面) | 'backdrop' (背景)
    """
    target_id = item_id
    
    # 智能查找 SeriesId (如果请求的是单集封面，尝试返回剧集封面，更好看)
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
        # 请求 Emby 图片
        resp = requests.get(f"{EMBY_HOST}/emby/Items/{target_id}{suffix}", timeout=3)
        if resp.status_code == 200:
            return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
        
        # 如果失败，且我们刚才替换过 ID，尝试用原始 ID 再试一次
        if target_id != item_id:
            resp_fallback = requests.get(f"{EMBY_HOST}/emby/Items/{item_id}{suffix}", timeout=3)
            if resp_fallback.status_code == 200:
                return Response(content=resp_fallback.content, media_type=resp_fallback.headers.get("Content-Type", "image/jpeg"))
                
    except: pass
    
    # 彻底失败，返回默认图
    return RedirectResponse(FALLBACK_IMAGE_URL)

# === 其他辅助接口 (保持兼容性) ===
@app.get("/api/stats/chart")
async def api_chart_stats(user_id: Optional[str] = None, dimension: str = 'month'):
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
            for r in rows: data[r['Label']] = int(r['Duration'])
        return {"status": "success", "data": data}
    except: return {"status": "error", "data": {}}

@app.get("/api/stats/user_details")
async def api_user_details(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all':
            where += " AND UserId = ?"
            params.append(user_id)
        hourly_res = query_db(f"SELECT strftime('%H', DateCreated) as Hour, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Hour ORDER BY Hour", params)
        hourly_data = {str(i).zfill(2): 0 for i in range(24)}
        if hourly_res:
            for r in hourly_res: hourly_data[r['Hour']] = r['Plays']
        device_res = query_db(f"SELECT COALESCE(DeviceName, ClientName, 'Unknown') as Device, COUNT(*) as Plays FROM PlaybackActivity {where} GROUP BY Device ORDER BY Plays DESC", params)
        logs_res = query_db(f"SELECT DateCreated, ItemName, PlayDuration, COALESCE(DeviceName, ClientName) as Device, UserId FROM PlaybackActivity {where} ORDER BY DateCreated DESC LIMIT 100", params)
        user_map = get_user_map()
        logs_data = []
        if logs_res:
            for r in logs_res:
                l = dict(r)
                l['UserName'] = user_map.get(l['UserId'], "User")
                logs_data.append(l)
        return {"status": "success", "data": {"hourly": hourly_data, "devices": [dict(r) for r in device_res] if device_res else [], "logs": logs_data}}
    except: return {"status": "error", "data": {"hourly": {}, "devices": [], "logs": []}}

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
    except: return {"status": "error", "data": []}

@app.get("/api/stats/badges")
async def api_badges(user_id: Optional[str] = None):
    try:
        where, params = "WHERE 1=1", []
        if user_id and user_id != 'all': where += " AND UserId = ?"; params.append(user_id)
        badges = []
        night_res = query_db(f"SELECT COUNT(*) as c FROM PlaybackActivity {where} AND strftime('%H', DateCreated) BETWEEN '02' AND '05'", params)
        if night_res and night_res[0]['c'] > 5:
            badges.append({"id": "night", "name": "修仙党", "icon": "fa-moon", "color": "text-purple-500", "bg": "bg-purple-100", "desc": "深夜是灵魂最自由的时刻"})
        dur_res = query_db(f"SELECT SUM(PlayDuration) as c FROM PlaybackActivity {where}", params)
        if dur_res and dur_res[0]['c'] and dur_res[0]['c'] > 360000:
            badges.append({"id": "king", "name": "影视肝帝", "icon": "fa-crown", "color": "text-yellow-600", "bg": "bg-yellow-100", "desc": "阅片量惊人"})
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