from fastapi import APIRouter, Request
from app.schemas.models import UserUpdateModel, NewUserModel
from app.core.config import cfg
from app.core.database import query_db
import requests
import datetime
import json

router = APIRouter()

@router.get("/api/manage/users")
def api_manage_users(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code != 200: return {"status": "error", "message": "Emby API Error"}
        emby_users = res.json()
        meta_rows = query_db("SELECT * FROM users_meta")
        meta_map = {r['user_id']: dict(r) for r in meta_rows} if meta_rows else {}
        final_list = []
        for u in emby_users:
            uid = u['Id']; meta = meta_map.get(uid, {}); policy = u.get('Policy', {})
            final_list.append({
                "Id": uid, "Name": u['Name'], "LastLoginDate": u.get('LastLoginDate'),
                "IsDisabled": policy.get('IsDisabled', False), "IsAdmin": policy.get('IsAdministrator', False),
                "ExpireDate": meta.get('expire_date'), "Note": meta.get('note'), "PrimaryImageTag": u.get('PrimaryImageTag')
            })
        return {"status": "success", "data": final_list}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/update")
def api_manage_user_update(data: UserUpdateModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    print(f"📝 Update User: {data.user_id}")
    
    try:
        # 1. 更新数据库有效期 (本地逻辑)
        if data.expire_date is not None:
            exist = query_db("SELECT 1 FROM users_meta WHERE user_id = ?", (data.user_id,), one=True)
            if exist: query_db("UPDATE users_meta SET expire_date = ? WHERE user_id = ?", (data.expire_date, data.user_id))
            else: query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (data.user_id, data.expire_date, datetime.datetime.now().isoformat()))
        
        # 2. 🔥 组合拳第一步：净化账号 (斩断云端关联)
        # 必须先获取用户详情，检查是否有 ConnectUserId残留
        user_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
        if user_res.status_code == 200:
            user_dto = user_res.json()
            # 如果发现有云端绑定ID，强制清除
            if user_dto.get("ConnectUserId") or user_dto.get("ConnectLinkType"):
                print(f"🧹 Cleaning Emby Connect link for {data.user_id}...")
                user_dto["ConnectUserId"] = None
                user_dto["ConnectLinkType"] = None
                # 更新用户资料 (POST /Users/{Id})
                clean_res = requests.post(f"{host}/emby/Users/{data.user_id}?api_key={key}", json=user_dto)
                print(f"🧹 Cleanse Result: {clean_res.status_code}")

        # 3. 🔥 组合拳第二步：解禁与重置策略
        if data.is_disabled is not None:
            print(f"🔧 Updating Policy for {data.user_id}...")
            # 获取最新策略（防止覆盖）
            p_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if p_res.status_code == 200:
                policy = p_res.json().get('Policy', {})
                policy['IsDisabled'] = data.is_disabled
                # 只有在启用时才重置错误次数，防止死锁
                if not data.is_disabled:
                    policy['LoginAttemptsBeforeLockout'] = -1 
                requests.post(f"{host}/emby/Users/{data.user_id}/Policy?api_key={key}", json=policy)

        # 4. 🔥 组合拳第三步：管理员强制改密
        if data.password:
            print(f"🔑 Admin Force Setting Password for {data.user_id}...")
            # 关键参数：ResetPassword=True。
            # 因为前面已经断开了云端关联，这次本地改密应该会被正确执行 (耗时 > 1ms)
            payload = {
                "Id": data.user_id,
                "NewPassword": data.password, 
                "ResetPassword": True 
            }
            r = requests.post(f"{host}/emby/Users/{data.user_id}/Password?api_key={key}", json=payload)
            
            print(f"🔑 Emby Response: {r.status_code} - {r.text}")
            if r.status_code not in [200, 204]:
                return {"status": "error", "message": f"改密失败: {r.text}"}

        return {"status": "success", "message": "更新成功"}
    except Exception as e: 
        print(f"❌ Error: {e}")
        return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/new")
def api_manage_user_new(data: NewUserModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    print(f"📝 New User: {data.name}")
    
    try:
        # 1. 创建用户
        res = requests.post(f"{host}/emby/Users/New?api_key={key}", json={"Name": data.name})
        if res.status_code != 200: return {"status": "error", "message": f"创建失败: {res.text}"}
        new_id = res.json()['Id']
        
        # 2. 立即初始化策略 (解禁)
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json={"IsDisabled": False, "LoginAttemptsBeforeLockout": -1})
        
        # 3. 设置初始密码
        if data.password:
            print(f"🔑 Setting initial password for {new_id}...")
            payload = {
                "Id": new_id,
                "NewPassword": data.password,
                "ResetPassword": True
            }
            requests.post(f"{host}/emby/Users/{new_id}/Password?api_key={key}", json=payload)

        # 4. 记录有效期
        if data.expire_date:
            query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (new_id, data.expire_date, datetime.datetime.now().isoformat()))
            
        return {"status": "success", "message": "用户创建成功"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.delete("/api/manage/user/{user_id}")
def api_manage_user_delete(user_id: str, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        res = requests.delete(f"{host}/emby/Users/{user_id}?api_key={key}")
        if res.status_code in [200, 204]:
            query_db("DELETE FROM users_meta WHERE user_id = ?", (user_id,))
            return {"status": "success", "message": "用户已删除"}
        return {"status": "error", "message": "删除失败"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.get("/api/users")
def api_get_users():
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if not key: return {"status": "error"}
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code == 200:
            users = res.json(); hidden = cfg.get("hidden_users") or []; data = []
            for u in users: data.append({"UserId": u['Id'], "UserName": u['Name'], "IsHidden": u['Id'] in hidden})
            data.sort(key=lambda x: x['UserName'])
            return {"status": "success", "data": data}
        return {"status": "success", "data": []}
    except Exception as e: return {"status": "error", "message": str(e)}