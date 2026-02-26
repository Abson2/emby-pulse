from fastapi import APIRouter, Request, Response, UploadFile, File, Form
from app.schemas.models import UserUpdateModel, NewUserModel, InviteGenModel
from app.core.config import cfg
from app.core.database import query_db
import requests
import datetime
import secrets
import base64  # 🔥 引入 base64

router = APIRouter()

# 🔥 自动检查过期用户并禁用 (保留功能)
def check_expired_users():
    try:
        key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
        if not key or not host: return
        
        # 1. 查出所有设置了过期时间的用户
        rows = query_db("SELECT user_id, expire_date FROM users_meta WHERE expire_date IS NOT NULL")
        if not rows: return
        
        now_str = datetime.datetime.now().strftime("%Y-%m-%d")
        
        for row in rows:
            if row['expire_date'] < now_str: # 已过期
                uid = row['user_id']
                try:
                    u_res = requests.get(f"{host}/emby/Users/{uid}?api_key={key}", timeout=5)
                    if u_res.status_code == 200:
                        user = u_res.json()
                        policy = user.get('Policy', {})
                        # 如果未禁用，则执行禁用
                        if not policy.get('IsDisabled', False):
                            print(f"🚫 Auto-Disabling Expired User: {user.get('Name')} (Expire: {row['expire_date']})")
                            policy['IsDisabled'] = True
                            requests.post(f"{host}/emby/Users/{uid}/Policy?api_key={key}", json=policy)
                except: pass
    except Exception as e:
        print(f"Check Expire Error: {e}")

@router.get("/api/manage/users")
def api_manage_users(request: Request):
    """
    获取用户列表及元数据
    """
    if not request.session.get("user"): return {"status": "error"}
    
    # 每次获取列表时，顺手检查一下过期状态
    check_expired_users()
    
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    
    # 获取公开地址，用于前端显示头像
    public_host = cfg.get("emby_public_host") or host
    if public_host.endswith('/'): public_host = public_host[:-1]
    
    try:
        res = requests.get(f"{host}/emby/Users?api_key={key}", timeout=5)
        if res.status_code != 200: return {"status": "error", "message": "Emby API Error"}
        emby_users = res.json()
        
        # 获取本地数据库中的扩展信息（过期时间、备注）
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
                "PrimaryImageTag": u.get('PrimaryImageTag') # 确保这个字段被传递
            })
            
        return {
            "status": "success", 
            "data": final_list, 
            "emby_url": public_host 
        }
    except Exception as e: return {"status": "error", "message": str(e)}

# 🔥 用户头像代理接口 (增加缓存控制)
@router.get("/api/user/image/{user_id}")
def get_user_avatar(user_id: str):
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    if not key or not host: return Response(status_code=404)
    
    try:
        # 尝试获取用户头像
        img_url = f"{host}/emby/Users/{user_id}/Images/Primary?api_key={key}&quality=90"
        res = requests.get(img_url, timeout=5)
        
        if res.status_code == 200:
            # 🔥 增加 Cache-Control 头，防止浏览器缓存旧头像
            return Response(
                content=res.content, 
                media_type="image/jpeg",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
            )
        else:
            return Response(status_code=404)
    except:
        return Response(status_code=404)

# 🔥🔥🔥 核心修复：修改用户头像接口 (Base64 转码)
@router.post("/api/manage/user/image")
async def api_update_user_image(
    request: Request,
    user_id: str = Form(...),
    url: str = Form(None),
    file: UploadFile = File(None)
):
    if not request.session.get("user"): return {"status": "error", "message": "Unauthorized"}
    
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    # 上传地址
    post_url = f"{host}/emby/Users/{user_id}/Images/Primary?api_key={key}"
    # 删除地址 (用于清理旧头像)
    delete_url = f"{host}/emby/Users/{user_id}/Images/Primary?api_key={key}"
    
    image_data = None
    content_type = "image/png" # 默认值
    
    try:
        # 1. 准备数据
        # 情况 A: 传的是 URL (DiceBear)
        if url:
            print(f"🖼️ Downloading avatar from: {url}")
            down_res = requests.get(url, timeout=10)
            if down_res.status_code == 200:
                image_data = down_res.content
                if 'Content-Type' in down_res.headers:
                    content_type = down_res.headers['Content-Type']
            else:
                return {"status": "error", "message": "无法下载该头像"}
        
        # 情况 B: 传的是文件
        elif file:
            print(f"📂 Receiving file upload: {file.filename}, Type: {file.content_type}")
            image_data = await file.read()
            content_type = file.content_type or "image/jpeg"
            
        if not image_data or len(image_data) == 0:
            return {"status": "error", "message": "图片数据为空"}

        # 🔥 关键修复：Emby 要求 Body 必须是 Base64 字符串
        b64_data = base64.b64encode(image_data)
        
        print(f"🚀 Uploading to Emby (Base64)... Original Size: {len(image_data)} bytes")

        # 2. 先删除旧头像 (防止 Emby 缓存不更新)
        try:
            requests.delete(delete_url)
        except: pass 

        # 3. 上传新头像
        # 虽然 Body 是 Base64，但 Content-Type 依然建议传图片类型，或者 application/octet-stream
        headers = {"Content-Type": content_type}
        up_res = requests.post(post_url, data=b64_data, headers=headers)
        
        if up_res.status_code in [200, 204]:
            print("✅ Avatar updated successfully.")
            return {"status": "success"}
        else:
            print(f"❌ Emby Upload Failed: {up_res.status_code} - {up_res.text}")
            return {"status": "error", "message": f"Emby 返回错误: {up_res.status_code}"}

    except Exception as e:
        print(f"❌ Exception: {e}")
        return {"status": "error", "message": str(e)}

# 生成邀请码接口 (保留功能)
@router.post("/api/manage/invite/gen")
def api_gen_invite(data: InviteGenModel, request: Request):
    if not request.session.get("user"): return {"status": "error"}
    try:
        # 生成 6 位随机码
        code = secrets.token_hex(3) 
        created_at = datetime.datetime.now().isoformat()
        
        query_db("INSERT INTO invitations (code, days, created_at) VALUES (?, ?, ?)", 
                 (code, data.days, created_at))
                 
        return {"status": "success", "code": code}
    except Exception as e: return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/update")
def api_manage_user_update(data: UserUpdateModel, request: Request):
    """
    更新用户：支持修改 密码、停用状态、过期时间
    """
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    print(f"📝 Update User Request: {data.user_id}")
    
    try:
        # 1. 更新数据库有效期 (本地业务)
        if data.expire_date is not None:
            # 如果传的是空字符串，转为 None 存入数据库（表示永久）
            expire_val = data.expire_date if data.expire_date else None
            
            exist = query_db("SELECT 1 FROM users_meta WHERE user_id = ?", (data.user_id,), one=True)
            if exist: query_db("UPDATE users_meta SET expire_date = ? WHERE user_id = ?", (expire_val, data.user_id))
            else: query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", (data.user_id, expire_val, datetime.datetime.now().isoformat()))
        
        # 2. 修改密码
        if data.password:
            print(f"🔐 Resetting Password for {data.user_id}")
            pwd_res = requests.post(f"{host}/emby/Users/{data.user_id}/Password?api_key={key}", 
                                  json={"Id": data.user_id, "NewPw": data.password})
            if pwd_res.status_code not in [200, 204]:
                return {"status": "error", "message": "密码修改失败，请检查日志"}

        # 3. 刷新策略 (处理 停用/启用)
        if data.is_disabled is not None:
            print(f"🔧 Updating Policy (IsDisabled={data.is_disabled})...")
            p_res = requests.get(f"{host}/emby/Users/{data.user_id}?api_key={key}")
            if p_res.status_code == 200:
                policy = p_res.json().get('Policy', {})
                policy['IsDisabled'] = data.is_disabled
                # 如果是启用，重置错误次数，防止因为之前的尝试被锁
                if not data.is_disabled:
                    policy['LoginAttemptsBeforeLockout'] = -1 
                
                r = requests.post(f"{host}/emby/Users/{data.user_id}/Policy?api_key={key}", json=policy)
                if r.status_code != 204:
                    print(f"⚠️ Policy Update Warning: {r.status_code}")

        return {"status": "success", "message": "用户信息已更新"}
    except Exception as e: 
        print(f"❌ Error: {e}")
        return {"status": "error", "message": str(e)}

@router.post("/api/manage/user/new")
def api_manage_user_new(data: NewUserModel, request: Request):
    """
    新建用户：创建用户 + 设置密码 + 初始化策略 + 设置过期时间
    """
    if not request.session.get("user"): return {"status": "error"}
    key = cfg.get("emby_api_key"); host = cfg.get("emby_host")
    print(f"📝 New User: {data.name}")
    try:
        # 1. 创建用户
        res = requests.post(f"{host}/emby/Users/New?api_key={key}", json={"Name": data.name})
        if res.status_code != 200: return {"status": "error", "message": f"创建失败: {res.text}"}
        new_id = res.json()['Id']
        
        # 2. 设置密码 (如果提供了)
        if data.password:
            requests.post(f"{host}/emby/Users/{new_id}/Password?api_key={key}", json={"Id": new_id, "NewPw": data.password})
        
        # 3. 立即初始化策略 (防止默认被禁用)
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json={"IsDisabled": False, "LoginAttemptsBeforeLockout": -1})
        
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
    """
    简易用户列表 (用于下拉框等)
    """
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