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
    """
    检查并自动禁用已过期的 Emby 用户
    """
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
                            print(f"🚫 账号已到期，自动禁用: {user.get('Name')} (有效期至: {row['expire_date']})")
                            policy['IsDisabled'] = True
                            requests.post(f"{host}/emby/Users/{uid}/Policy?api_key={key}", json=policy)
                except Exception as e:
                    print(f"处理过期用户 {uid} 失败: {e}")
    except Exception as e:
        print(f"Check Expire Error: {e}")

@router.get("/api/manage/libraries")
def api_get_libraries(request: Request):
    """
    获取 Emby 媒体库列表 (提取 GUID 以确保权限生效)
    """
    if not request.session.get("user"):
        return {"status": "error", "message": "未授权"}
    
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    try:
        # 使用 VirtualFolders 接口获取
        res = requests.get(f"{host}/emby/Library/VirtualFolders?api_key={key}", timeout=5)
        if res.status_code == 200:
            # 精准提取 Guid，这解决了 Emby 同步不上的致命问题
            libs = []
            for item in res.json():
                if "Guid" in item:
                    libs.append({
                        "Id": item["Guid"],
                        "Name": item["Name"]
                    })
            return {"status": "success", "data": libs}
        return {"status": "error", "message": "Emby API 返回错误"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/api/manage/users")
def api_manage_users(request: Request):
    """
    获取后台管理的用户列表 (包含本地过期时间数据)
    """
    if not request.session.get("user"):
        return {"status": "error", "message": "未授权"}
    
    check_expired_users()
    
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    public_host = cfg.get("emby_public_host") or host
    if public_host.endswith('/'):
        public_host = public_host[:-1]
    
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code != 200:
            return {"status": "error", "message": "无法连接 Emby 服务器"}
        
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
                # 🔥 列表带上子文件夹黑名单
                "ExcludedSubFolders": policy.get('ExcludedSubFolders', [])
            })
            
        return {
            "status": "success", 
            "data": final_list, 
            "emby_url": public_host 
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/api/manage/user/{user_id}")
def api_get_single_user(user_id: str, request: Request):
    """
    获取单个用户的完整真实数据 (解决 Emby 列表接口隐藏库权限的问题)
    """
    if not request.session.get("user"):
        return {"status": "error"}
        
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    try:
        res = requests.get(f"{host}/emby/Users/{user_id}?api_key={key}", timeout=5)
        if res.status_code == 200:
            user_data = res.json()
            policy = user_data.get('Policy', {})
            return {
                "status": "success", 
                "data": {
                    "Id": user_data['Id'],
                    "Name": user_data['Name'],
                    "EnableAllFolders": policy.get('EnableAllFolders', True),
                    "EnabledFolders": policy.get('EnabledFolders', []),
                    "ExcludedSubFolders": policy.get('ExcludedSubFolders', [])
                }
            }
        return {"status": "error", "message": "Emby 找不到该用户"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/api/user/image/{user_id}")
def get_user_avatar(user_id: str):
    """
    代理获取 Emby 头像，解决跨域及缓存问题
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    if not key or not host:
        return Response(status_code=404)
    
    try:
        img_url = f"{host}/emby/Users/{user_id}/Images/Primary?api_key={key}&quality=90"
        res = requests.get(img_url, timeout=5)
        if res.status_code == 200:
            return Response(
                content=res.content, 
                media_type="image/jpeg", 
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
            )
        else:
            return Response(status_code=404)
    except:
        return Response(status_code=404)

@router.post("/api/manage/user/image")
async def api_update_user_image(
    request: Request, 
    user_id: str = Form(...), 
    url: str = Form(None), 
    file: UploadFile = File(None)
):
    """
    更新用户头像 (支持 URL 下载和本地文件上传)
    """
    if not request.session.get("user"):
        return {"status": "error", "message": "Unauthorized"}
        
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    image_data = None
    content_type = "image/png"
    
    try:
        if url:
            # 从远程 URL 下载
            down_res = requests.get(url, timeout=10)
            if down_res.status_code == 200:
                image_data = down_res.content
                if 'Content-Type' in down_res.headers:
                    content_type = down_res.headers['Content-Type']
            else:
                return {"status": "error", "message": "无法下载该头像内容"}
        elif file:
            # 读取上传的文件
            image_data = await file.read()
            content_type = file.content_type or "image/jpeg"
            
        if not image_data or len(image_data) == 0:
            return {"status": "error", "message": "图片数据为空"}
            
        # 转换为 Base64
        b64_data = base64.b64encode(image_data)
        
        # 先删除旧头像
        try:
            requests.delete(f"{host}/emby/Users/{user_id}/Images/Primary?api_key={key}")
        except:
            pass 

        # 发送 POST 请求上传
        headers = {"Content-Type": content_type}
        up_res = requests.post(
            f"{host}/emby/Users/{user_id}/Images/Primary?api_key={key}", 
            data=b64_data, 
            headers=headers
        )
        
        if up_res.status_code in [200, 204]:
            return {"status": "success"}
        else:
            return {"status": "error", "message": f"Emby 返回错误码: {up_res.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/api/manage/invite/gen")
def api_gen_invite(data: InviteGenModel, request: Request):
    """
    生成注册邀请码
    """
    if not request.session.get("user"):
        return {"status": "error"}
    try:
        code = secrets.token_hex(3) 
        created_at = datetime.datetime.now().isoformat()
        query_db(
            "INSERT INTO invitations (code, days, created_at, template_user_id) VALUES (?, ?, ?, ?)", 
            (code, data.days, created_at, data.template_user_id)
        )
        return {"status": "success", "code": code}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/update")
def api_manage_user_update(data: UserUpdateModel, request: Request):
    """
    更新用户信息及权限 (核心修复：支持镜像同步子文件夹)
    """
    if not request.session.get("user"):
        return {"status": "error"}
        
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    try:
        # 1. 更新本地数据库中的有效期
        if data.expire_date is not None:
            expire_val = data.expire_date if data.expire_date else None
            exist = query_db("SELECT 1 FROM users_meta WHERE user_id = ?", (data.user_id,), one=True)
            if exist:
                query_db("UPDATE users_meta SET expire_date = ? WHERE user_id = ?", (expire_val, data.user_id))
            else:
                query_db(
                    "INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", 
                    (data.user_id, expire_val, datetime.datetime.now().isoformat())
                )
        
        # 2. 更新密码
        if data.password:
            requests.post(
                f"{host}/emby/Users/{data.user_id}/Password?api_key={key}", 
                json={"Id": data.user_id, "NewPw": data.password}
            )

        # 3. 更新媒体库权限 (Policy)
        if any(x is not None for x in [data.is_disabled, data.enable_all_folders, data.enabled_folders, data.excluded_sub_folders]):
            p_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if p_res.status_code == 200:
                policy = p_res.json().get('Policy', {})
                
                # 更新禁用状态
                if data.is_disabled is not None:
                    policy['IsDisabled'] = data.is_disabled
                    if not data.is_disabled:
                        policy['LoginAttemptsBeforeLockout'] = -1 
                
                # 更新媒体库白名单
                if data.enable_all_folders is not None:
                    policy['EnableAllFolders'] = bool(data.enable_all_folders)
                    if policy['EnableAllFolders']:
                        policy['EnabledFolders'] = [] 
                    else:
                        policy['EnabledFolders'] = [str(x) for x in data.enabled_folders] if data.enabled_folders is not None else []
                
                # 🔥 关键修复：写入子文件夹排除项 (黑名单模式)
                if data.excluded_sub_folders is not None:
                    policy['ExcludedSubFolders'] = data.excluded_sub_folders
                
                # 深度净化脏数据，防止 Emby 拒绝保存
                junk_keys = [
                    'BlockedMediaFolders', 'BlockedChannels', 'EnableAllChannels', 
                    'EnabledChannels', 'BlockedTags', 'AllowedTags'
                ]
                for k in junk_keys:
                    policy.pop(k, None)
                
                headers = {"Content-Type": "application/json", "X-Emby-Token": key}
                up_res = requests.post(
                    f"{host}/emby/Users/{data.user_id}/Policy?api_key={key}", 
                    json=policy, 
                    headers=headers
                )
                
                if up_res.status_code not in [200, 204]:
                    return {"status": "error", "message": f"Emby权限保存失败: {up_res.text}"}

        return {"status": "success", "message": "用户信息更新成功"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/new")
def api_manage_user_new(data: NewUserModel, request: Request):
    """
    新建用户并初始化权限模板
    """
    if not request.session.get("user"):
        return {"status": "error"}
        
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    
    try:
        # 创建基础用户
        res = requests.post(f"{host}/emby/Users/New?api_key={key}", json={"Name": data.name})
        if res.status_code != 200:
            return {"status": "error", "message": f"Emby拒绝创建: {res.text}"}
        new_id = res.json()['Id']
        
        # 设置初始密码
        if data.password:
            requests.post(
                f"{host}/emby/Users/{new_id}/Password?api_key={key}", 
                json={"Id": new_id, "NewPw": data.password}
            )
        
        # 获取新用户的 Policy 对象进行编辑
        p_res = requests.get(f"{host}/emby/Users/{new_id}?api_key={key}")
        policy = p_res.json().get('Policy', {}) if p_res.status_code == 200 else {}
        policy['IsDisabled'] = False
        policy['LoginAttemptsBeforeLockout'] = -1
        
        # 🔥 如果指定了模板，镜像复制所有库权限
        if data.template_user_id:
            src_res = requests.get(f"{host}/emby/Users/{data.template_user_id}?api_key={key}", timeout=5)
            if src_res.status_code == 200:
                src_policy = src_res.json().get('Policy', {})
                policy['EnableAllFolders'] = src_policy.get('EnableAllFolders', True)
                policy['EnabledFolders'] = src_policy.get('EnabledFolders', [])
                # 🔥 新建也带上子文件夹黑名单
                policy['ExcludedSubFolders'] = src_policy.get('ExcludedSubFolders', [])
        
        # 净化并保存
        junk_keys = ['BlockedMediaFolders', 'BlockedChannels', 'EnableAllChannels', 'EnabledChannels']
        for k in junk_keys:
            policy.pop(k, None)

        headers = {"Content-Type": "application/json", "X-Emby-Token": key}
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json=policy, headers=headers)
        
        # 记录本地有效期
        if data.expire_date:
            query_db(
                "INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", 
                (new_id, data.expire_date, datetime.datetime.now().isoformat())
            )
            
        return {"status": "success", "message": "用户创建及模板同步成功"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.delete("/api/manage/user/{user_id}")
def api_manage_user_delete(user_id: str, request: Request):
    """
    删除用户并清理本地数据库
    """
    if not request.session.get("user"):
        return {"status": "error"}
        
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    try:
        res = requests.delete(f"{host}/emby/Users/{user_id}?api_key={key}")
        if res.status_code in [200, 204]:
            query_db("DELETE FROM users_meta WHERE user_id = ?", (user_id,))
            return {"status": "success", "message": "用户已彻底删除"}
        return {"status": "error", "message": "Emby 接口删除失败"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/api/users")
def api_get_users():
    """
    给普通功能（非管理）使用的简单用户列表
    """
    key = cfg.get("emby_api_key")
    host = cfg.get("emby_host")
    if not key:
        return {"status": "error"}
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code == 200:
            users_raw = res.json()
            hidden = cfg.get("hidden_users") or []
            data = []
            for u in users_raw:
                data.append({
                    "UserId": u['Id'], 
                    "UserName": u['Name'], 
                    "IsHidden": u['Id'] in hidden
                })
            data.sort(key=lambda x: x['UserName'])
            return {"status": "success", "data": data}
        return {"status": "success", "data": []}
    except Exception as e:
        return {"status": "error", "message": str(e)}