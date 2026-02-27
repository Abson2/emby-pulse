from fastapi import APIRouter, Request, Response, UploadFile, File, Form
from app.schemas.models import UserUpdateModel, NewUserModel, InviteGenModel
from app.core.config import cfg
from app.core.database import query_db
import requests
import datetime
import secrets
import base64

router = APIRouter()

def check_expired_users():
    try:
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return
        
        rows = query_db("SELECT user_id, expire_date FROM users_meta WHERE expire_date IS NOT NULL")
        if not rows: return
        
        now_str = datetime.datetime.now().strftime("%Y-%m-%d")
        
        for row in rows:
            if row['expire_date'] < now_str: 
                uid = row['user_id']
                try:
                    u_res = requests.get(f"{host}/emby/Users/{uid}?api_key={key}", timeout=5)
                    if u_res.status_code == 200:
                        user = u_res.json()
                        policy = user.get('Policy', {})
                        if not policy.get('IsDisabled', False):
                            print(f"🚫 Auto-Disabling Expired User: {user.get('Name')} (Expire: {row['expire_date']})")
                            policy['IsDisabled'] = True
                            requests.post(f"{host}/emby/Users/{uid}/Policy?api_key={key}", json=policy)
                except: pass
    except Exception as e:
        print(f"Check Expire Error: {e}")

# 🔥🔥🔥 核心修复：改用 VirtualFolders 获取媒体库的 32 位 GUID (ItemId)
@router.get("/api/manage/libraries")
def api_get_libraries(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        # 换用 VirtualFolders 接口，它会返回完整的库配置，包含 32 位 GUID
        res = requests.get(f"{host}/emby/Library/VirtualFolders?api_key={key}", timeout=5)
        if res.status_code == 200:
            # Emby 的 VirtualFolders 返回的是一个数组，里面有 ItemId
            libs = [{"Id": item["ItemId"], "Name": item["Name"]} for item in res.json() if "ItemId" in item]
            return {"status": "success", "data": libs}
        return {"status": "error", "message": "获取媒体库失败"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.get("/api/manage/users")
def api_manage_users(request: Request):
    if not request.session.get("user"): return {"status": "error"}
    
    check_expired_users()
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    public_host = cfg.get("emby_public_host") or host
    if public_host.endswith('/'): public_host = public_host[:-1]
    
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code != 200: return {"status": "error", "message": "Emby API Error"}
        emby_users = res.json()
        
        meta_rows = query_db("SELECT * FROM users_meta")
        meta_map = {r['user_id']: dict(r) for r in meta_rows} if meta_rows else {}
        
        final_list = []
        for u in emby_users:
            uid = u['Id']
            meta = meta_map.get(uid, {})
            policy = u.get('Policy', {})
            final_list.append({
                "Id": uid, 
                "Name": u['Name'], 
                "LastLoginDate": u.get('LastLoginDate'),
                "IsDisabled": policy.get('IsDisabled', False), 
                "IsAdmin": policy.get('IsAdministrator', False),
                "ExpireDate": meta.get('expire_date'), 
                "Note": meta.get('note'), 
                "PrimaryImageTag": u.get('PrimaryImageTag'),
                "EnableAllFolders": policy.get('EnableAllFolders', True),
                "EnabledFolders": policy.get('EnabledFolders', [])
            })
            
        return {
            "status": "success", 
            "data": final_list, 
            "emby_url": public_host 
        }
    except Exception as e: return {"status": "error", "message": str(e)}

@router.get("/api/manage/user/{user_id}")
def api_get_single_user(user_id: str, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        res = requests.get(f"{host}/emby/Users/{user_id}?api_key={key}", timeout=5)
        if res.status_code == 200:
            user_data = res.json()
            policy = user_data.get('Policy', {})
            return {"status": "success", "data": {
                "Id": user_data['Id'],
                "Name": user_data['Name'],
                "EnableAllFolders": policy.get('EnableAllFolders', True),
                "EnabledFolders": policy.get('EnabledFolders', [])
            }}
        return {"status": "error", "message": "获取用户详情失败"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.get("/api/user/image/{user_id}")
def get_user_avatar(user_id: str):
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if not key or not host: return Response(status_code=404)
    
    try:
        img_url = f"{host}/emby/Users/{user_id}/Images/Primary?api_key={key}&quality=90"
        res = requests.get(img_url, timeout=5)
        if res.status_code == 200:
            return Response(content=res.content, media_type="image/jpeg", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        else: return Response(status_code=404)
    except: return Response(status_code=404)

@router.post("/api/manage/user/image")
async def api_update_user_image(request: Request, user_id: str = Form(...), url: str = Form(None), file: UploadFile = File(None)):
    if not request.session.get("user"): return {"status": "error", "message": "Unauthorized"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    post_url = f"{host}/emby/Users/{user_id}/Images/Primary?api_key={key}"
    delete_url = f"{host}/emby/Users/{user_id}/Images/Primary?api_key={key}"
    image_data = None; content_type = "image/png"
    
    try:
        if url:
            down_res = requests.get(url, timeout=10)
            if down_res.status_code == 200:
                image_data = down_res.content
                if 'Content-Type' in down_res.headers: content_type = down_res.headers['Content-Type']
            else: return {"status": "error", "message": "无法下载该头像"}
        elif file:
            image_data = await file.read(); content_type = file.content_type or "image/jpeg"
            
        if not image_data or len(image_data) == 0: return {"status": "error", "message": "图片数据为空"}
        b64_data = base64.b64encode(image_data)
        
        try: requests.delete(delete_url)
        except: pass 

        headers = {"Content-Type": content_type}
        up_res = requests.post(post_url, data=b64_data, headers=headers)
        
        if up_res.status_code in [200, 204]: return {"status": "success"}
        else: return {"status": "error", "message": f"Emby 返回错误: {up_res.status_code}"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/manage/invite/gen")
def api_gen_invite(data: InviteGenModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    try:
        code = secrets.token_hex(3) 
        created_at = datetime.datetime.now().isoformat()
        query_db("INSERT INTO invitations (code, days, created_at, template_user_id) VALUES (?, ?, ?, ?)", (code, data.days, created_at, data.template_user_id))
        return {"status": "success", "code": code}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/update")
def api_manage_user_update(data: UserUpdateModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    
    try:
        if data.expire_date is not None:
            expire_val = data.expire_date if data.expire_date else None
            exist = query_db("SELECT 1 FROM users_meta WHERE user_id = ?", (data.user_id,), one=True)
            if exist: query_db("UPDATE users_meta SET expire_date = ? WHERE user_id = ?", (expire_val, data.user_id))
            else: query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (data.user_id, expire_val, datetime.datetime.now().isoformat()))
        
        if data.password:
            pwd_res = requests.post(f"{host}/emby/Users/{data.user_id}/Password?api_key={key}", json={"Id": data.user_id, "NewPw": data.password})
            if pwd_res.status_code not in [200, 204]: return {"status": "error", "message": "密码修改失败，请检查日志"}

        if data.is_disabled is not None or data.enable_all_folders is not None or data.enabled_folders is not None:
            p_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if p_res.status_code == 200:
                policy = p_res.json().get('Policy', {})
                
                if data.is_disabled is not None:
                    policy['IsDisabled'] = data.is_disabled
                    if not data.is_disabled: policy['LoginAttemptsBeforeLockout'] = -1 
                
                if data.enable_all_folders is not None:
                    policy['EnableAllFolders'] = bool(data.enable_all_folders)
                    if policy['EnableAllFolders']:
                        policy['EnabledFolders'] = [] 
                    else:
                        policy['EnabledFolders'] = [str(x) for x in data.enabled_folders] if data.enabled_folders else []
                
                junk_keys = ['BlockedMediaFolders', 'BlockedChannels', 'EnableAllChannels', 'EnabledChannels', 'BlockedTags', 'AllowedTags']
                for k in junk_keys: policy.pop(k, None)
                
                # 打印真正的发送负载以供确认
                print(f"🚀 [DEBUG] Cleaned Policy Update -> EnableAllFolders: {policy.get('EnableAllFolders')}, EnabledFolders: {policy.get('EnabledFolders')}")
                
                headers = {"Content-Type": "application/json", "X-Emby-Token": key}
                up_res = requests.post(f"{host}/emby/Users/{data.user_id}/Policy?api_key={key}", json=policy, headers=headers)
                
                if up_res.status_code not in [200, 204]:
                    return {"status": "error", "message": f"Emby拒绝了权限更新 (HTTP {up_res.status_code}): {up_res.text}"}

        return {"status": "success", "message": "用户信息已更新"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/new")
def api_manage_user_new(data: NewUserModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    
    try:
        res = requests.post(f"{host}/emby/Users/New?api_key={key}", json={"Name": data.name})
        if res.status_code != 200: return {"status": "error", "message": f"创建失败: {res.text}"}
        new_id = res.json()['Id']
        
        if data.password: requests.post(f"{host}/emby/Users/{new_id}/Password?api_key={key}", json={"Id": new_id, "NewPw": data.password})
        
        p_res = requests.get(f"{host}/emby/Users/{new_id}?api_key={key}")
        policy = p_res.json().get('Policy', {}) if p_res.status_code == 200 else {}
        policy['IsDisabled'] = False; policy['LoginAttemptsBeforeLockout'] = -1
        
        if data.template_user_id:
            src_res = requests.get(f"{host}/emby/Users/{data.template_user_id}?api_key={key}", timeout=5)
            if src_res.status_code == 200:
                src_policy = src_res.json().get('Policy', {})
                policy['EnableAllFolders'] = src_policy.get('EnableAllFolders', True)
                policy['EnabledFolders'] = src_policy.get('EnabledFolders', [])
        
        junk_keys = ['BlockedMediaFolders', 'BlockedChannels', 'EnableAllChannels', 'EnabledChannels', 'BlockedTags', 'AllowedTags']
        for k in junk_keys: policy.pop(k, None)

        headers = {"Content-Type": "application/json", "X-Emby-Token": key}
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json=policy, headers=headers)
        
        if data.expire_date: query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (new_id, data.expire_date, datetime.datetime.now().isoformat()))
            
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