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
    """ 扫描过期用户并自动在 Emby 端禁用 """
    try:
        key = cfg.get("emby_api_key")
        host = cfg.get("emby_host")
        if not key or not host:
            return
        
        rows = query_db("SELECT user_id, expire_date FROM users_meta WHERE expire_date IS NOT NULL")
        if not rows:
            return
        
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
                            print(f"🚫 账号已过期: {user.get('Name')} (有效期至: {row['expire_date']})")
                            policy['IsDisabled'] = True
                            requests.post(f"{host}/emby/Users/{uid}/Policy?api_key={key}", json=policy)
                except Exception as e:
                    print(f"处理过期用户错误: {e}")
    except Exception as e:
        print(f"Check Expire Error: {e}")

@router.get("/api/manage/libraries")
def api_get_libraries(request: Request):
    """ 获取媒体库，强制提取 GUID 解决权限失效问题 """
    if not request.session.get("user"):
        return {"status": "error"}
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    try:
        # 使用 VirtualFolders 获取，它包含 32 位 GUID
        res = requests.get(f"{host}/emby/Library/VirtualFolders?api_key={key}", timeout=5)
        if res.status_code == 200:
            # 🔥 必须使用 Guid 字段，这是 Emby 权限控制的唯一真理
            libs = [{"Id": item["Guid"], "Name": item["Name"]} for item in res.json() if "Guid" in item]
            return {"status": "success", "data": libs}
        return {"status": "error", "message": "Emby API 响应异常"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/api/manage/users")
def api_manage_users(request: Request):
    """ 管理员视图下的全量用户列表 """
    if not request.session.get("user"):
        return {"status": "error"}
    
    check_expired_users()
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    public_host = cfg.get("emby_public_host") or host
    if public_host.endswith('/'): public_host = public_host[:-1]
    
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code != 200:
            return {"status": "error", "message": "Emby 连接失败"}
        
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
                "EnabledFolders": policy.get('EnabledFolders', []),
                "ExcludedSubFolders": policy.get('ExcludedSubFolders', [])
            })
            
        return {"status": "success", "data": final_list, "emby_url": public_host}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/api/manage/user/{user_id}")
def api_get_single_user(user_id: str, request: Request):
    """ 获取单个用户的实时权限状态 (解决列表接口缓存/阉割问题) """
    if not request.session.get("user"):
        return {"status": "error"}
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    try:
        res = requests.get(f"{host}/emby/Users/{user_id}?api_key={key}", timeout=5)
        if res.status_code == 200:
            u_data = res.json()
            policy = u_data.get('Policy', {})
            return {
                "status": "success", 
                "data": {
                    "Id": u_data['Id'],
                    "Name": u_data['Name'],
                    "EnableAllFolders": policy.get('EnableAllFolders', True),
                    "EnabledFolders": policy.get('EnabledFolders', []),
                    "ExcludedSubFolders": policy.get('ExcludedSubFolders', [])
                }
            }
        return {"status": "error"}
    except:
        return {"status": "error"}

@router.get("/api/user/image/{user_id}")
def get_user_avatar(user_id: str):
    """ 头像代理接口 """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    try:
        res = requests.get(f"{host}/emby/Users/{user_id}/Images/Primary?api_key={key}&quality=90", timeout=5)
        if res.status_code == 200:
            return Response(content=res.content, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})
        return Response(status_code=404)
    except:
        return Response(status_code=404)

@router.post("/api/manage/user/image")
async def api_update_user_image(request: Request, user_id: str = Form(...), url: str = Form(None), file: UploadFile = File(None)):
    """ 更新头像 (支持文件上传和 URL) """
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        img_data = None; content_type = "image/png"
        if url:
            d_res = requests.get(url, timeout=10)
            if d_res.status_code == 200:
                img_data = d_res.content
                content_type = d_res.headers.get('Content-Type', 'image/png')
        elif file:
            img_data = await file.read()
            content_type = file.content_type or "image/jpeg"
            
        if not img_data: return {"status": "error", "message": "无图片"}
        b64 = base64.b64encode(img_data)
        requests.delete(f"{host}/emby/Users/{user_id}/Images/Primary?api_key={key}")
        requests.post(f"{host}/emby/Users/{user_id}/Images/Primary?api_key={key}", data=b64, headers={"Content-Type": content_type})
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/manage/invite/gen")
def api_gen_invite(data: InviteGenModel, request: Request):
    """ 🔥 批量生成邀请链接逻辑 """
    if not request.session.get("user"): return {"status": "error"}
    try:
        count = data.count if data.count and data.count > 0 else 1
        codes = []
        created_at = datetime.datetime.now().isoformat()
        for _ in range(count):
            code = secrets.token_hex(3)
            query_db(
                "INSERT INTO invitations (code, days, created_at, template_user_id) VALUES (?, ?, ?, ?)", 
                (code, data.days, created_at, data.template_user_id)
            )
            codes.append(code)
        return {"status": "success", "codes": codes}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/update")
def api_manage_user_update(data: UserUpdateModel, request: Request):
    """ 用户资料与媒体库权限 (GUID & 子文件夹同步) """
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        # 更新本地有效期
        if data.expire_date is not None:
            v = data.expire_date if data.expire_date else None
            exist = query_db("SELECT 1 FROM users_meta WHERE user_id = ?", (data.user_id,), one=True)
            if exist: query_db("UPDATE users_meta SET expire_date = ? WHERE user_id = ?", (v, data.user_id))
            else: query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (data.user_id, v, datetime.datetime.now().isoformat()))
        
        if data.password:
            requests.post(f"{host}/emby/Users/{data.user_id}/Password?api_key={key}", json={"Id": data.user_id, "NewPw": data.password})

        p_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
        if p_res.status_code == 200:
            p = p_res.json().get('Policy', {})
            if data.is_disabled is not None:
                p['IsDisabled'] = data.is_disabled
                if not data.is_disabled: p['LoginAttemptsBeforeLockout'] = -1
            if data.enable_all_folders is not None:
                p['EnableAllFolders'] = bool(data.enable_all_folders)
                if p['EnableAllFolders']: p['EnabledFolders'] = []
                else: p['EnabledFolders'] = [str(x) for x in data.enabled_folders] if data.enabled_folders is not None else []
            
            # 🔥 关键修复：写入子文件夹黑名单
            if data.excluded_sub_folders is not None:
                p['ExcludedSubFolders'] = data.excluded_sub_folders
            
            # 数据净化
            for k in ['BlockedMediaFolders','BlockedChannels','EnableAllChannels','EnabledChannels','BlockedTags','AllowedTags']: p.pop(k, None)
            
            requests.post(f"{host}/emby/Users/{data.user_id}/Policy?api_key={key}", json=p, headers={"Content-Type": "application/json", "X-Emby-Token": key})
            
        return {"status": "success", "message": "用户信息已同步至 Emby"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/new")
def api_manage_user_new(data: NewUserModel, request: Request):
    """ 创建新用户并镜像套用权限模板 """
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        res = requests.post(f"{host}/emby/Users/New?api_key={key}", json={"Name": data.name})
        if res.status_code != 200: return {"status": "error", "message": f"创建失败: {res.text}"}
        new_id = res.json()['Id']
        if data.password: requests.post(f"{host}/emby/Users/{new_id}/Password?api_key={key}", json={"Id": new_id, "NewPw": data.password})
        
        p = requests.get(f"{host}/emby/Users/{new_id}?api_key={key}").json().get('Policy', {})
        if data.template_user_id:
            src = requests.get(f"{host}/emby/Users/{data.template_user_id}?api_key={key}").json().get('Policy', {})
            p['EnableAllFolders'] = src.get('EnableAllFolders', True)
            p['EnabledFolders'] = src.get('EnabledFolders', [])
            p['ExcludedSubFolders'] = src.get('ExcludedSubFolders', [])
            
        junk = ['BlockedMediaFolders','BlockedChannels','EnableAllChannels','EnabledChannels']
        for k in junk: p.pop(k, None)
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json=p, headers={"X-Emby-Token": key})
        
        if data.expire_date: 
            query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (new_id, data.expire_date, datetime.datetime.now().isoformat()))
        return {"status": "success", "message": "创建成功"}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.delete("/api/manage/user/{user_id}")
def api_manage_user_delete(user_id: str, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if requests.delete(f"{host}/emby/Users/{user_id}?api_key={key}").status_code in [200, 204]:
        query_db("DELETE FROM users_meta WHERE user_id = ?", (user_id,))
        return {"status": "success"}
    return {"status": "error"}

@router.get("/api/users")
def api_get_users():
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code == 200:
            hidden = cfg.get("hidden_users") or []
            data = [{"UserId": u['Id'], "UserName": u['Name'], "IsHidden": u['Id'] in hidden} for u in res.json()]
            data.sort(key=lambda x: x['UserName'])
            return {"status": "success", "data": data}
        return {"status": "success", "data": []}
    except: return {"status": "error"}