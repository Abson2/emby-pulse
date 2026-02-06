from fastapi import APIRouter, Request
from app.schemas.models import UserUpdateModel, NewUserModel
from app.core.config import cfg
from app.core.database import query_db
import requests
import datetime
import json
import time

router = APIRouter()

# Emby 本地默认认证提供商的类名
DEFAULT_AUTH_PROVIDER = "Emby.Server.Implementations.Library.DefaultAuthenticationProvider"

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
    print(f"📝 Update User Request: {data.user_id}")
    
    try:
        # 1. 更新数据库有效期
        if data.expire_date is not None:
            exist = query_db("SELECT 1 FROM users_meta WHERE user_id = ?", (data.user_id,), one=True)
            if exist: query_db("UPDATE users_meta SET expire_date = ? WHERE user_id = ?", (data.expire_date, data.user_id))
            else: query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (data.user_id, data.expire_date, datetime.datetime.now().isoformat()))
        
        # 🔥 Step 1: 真正正确的净化 (修正了 LinkedUser 的 Bug)
        if data.password or data.is_disabled is not None:
            user_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if user_res.status_code == 200:
                user_dto = user_res.json()
                
                # 只有当数据不纯净时才执行
                is_dirty = (user_dto.get("AuthenticationProviderId") != DEFAULT_AUTH_PROVIDER) or \
                           user_dto.get("ConnectUserId") or \
                           user_dto.get("ConnectLinkType") == "LinkedUser" # 检查之前的错误标记

                if is_dirty or data.password:
                    print(f"🧹 [Step 1] Purging cloud auth (Setting to Local/Guest)...")
                    
                    user_dto["AuthenticationProviderId"] = DEFAULT_AUTH_PROVIDER
                    user_dto["ConnectUserId"] = ""  
                    user_dto["ConnectUserName"] = "" 
                    user_dto["ConnectLinkType"] = "" # 🔥 修正：必须是空字符串，不能是 LinkedUser
                    
                    # 移除干扰项
                    if "Password" in user_dto: del user_dto["Password"]
                    if "Configuration" in user_dto and "Password" in user_dto["Configuration"]: 
                        del user_dto["Configuration"]["Password"]

                    # 强制更新
                    clean_res = requests.post(f"{host}/emby/Users/{data.user_id}?api_key={key}", json=user_dto)
                    print(f"   -> Cleanse Status: {clean_res.status_code}")
                    
                    # 🔥 Step 1.5: 立即校验！看看 Emby 到底改没改
                    # 如果这一步验证失败，说明 Emby 数据库锁死，后续操作都是徒劳
                    verify_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
                    if verify_res.status_code == 200:
                        v_dto = verify_res.json()
                        provider = v_dto.get("AuthenticationProviderId")
                        print(f"   -> Verification: Provider is now '{provider}'")
                        if provider != DEFAULT_AUTH_PROVIDER:
                            print(f"   ⚠️ WARNING: Emby refused to change Provider. Password change will likely fail.")

        # 2. 刷新策略
        if data.is_disabled is not None:
            print(f"🔧 [Step 2] Updating Policy...")
            p_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if p_res.status_code == 200:
                policy = p_res.json().get('Policy', {})
                policy['IsDisabled'] = data.is_disabled
                if not data.is_disabled:
                    policy['LoginAttemptsBeforeLockout'] = -1 
                requests.post(f"{host}/emby/Users/{data.user_id}/Policy?api_key={key}", json=policy)

        # 3. 🔥 Step 3: 管理员强制改密 (简化版)
        # 既然账号已经本地化，我们不需要复杂的置空逻辑，直接用最标准的管理员重置
        if data.password:
            print(f"🔑 [Step 3] Force Admin Password Reset...")
            time.sleep(0.5) # 给数据库一点时间同步 Step 1 的变更
            
            payload = { 
                "Id": data.user_id, 
                "NewPassword": data.password, 
                "ResetPassword": True 
            }
            r = requests.post(f"{host}/emby/Users/{data.user_id}/Password?api_key={key}", json=payload)
            
            print(f"   -> Emby Final Response: {r.status_code}")
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
        # 1. 创建
        res = requests.post(f"{host}/emby/Users/New?api_key={key}", json={"Name": data.name})
        if res.status_code != 200: return {"status": "error", "message": f"创建失败: {res.text}"}
        new_id = res.json()['Id']
        
        # 2. 强制本地化
        user_res = requests.get(f"{host}/emby/Users/{new_id}?api_key={key}")
        if user_res.status_code == 200:
            user_dto = user_res.json()
            user_dto["AuthenticationProviderId"] = DEFAULT_AUTH_PROVIDER
            user_dto["ConnectUserId"] = ""
            user_dto["ConnectUserName"] = ""
            user_dto["ConnectLinkType"] = ""
            requests.post(f"{host}/emby/Users/{new_id}?api_key={key}", json=user_dto)

        # 3. 策略
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json={"IsDisabled": False, "LoginAttemptsBeforeLockout": -1})
        
        # 4. 设置初始密码
        if data.password:
            requests.post(f"{host}/emby/Users/{new_id}/Password?api_key={key}", json={"NewPassword": data.password, "ResetPassword": True})

        # 5. 记录
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