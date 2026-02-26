from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from app.core.config import cfg
from app.core.database import query_db
from app.schemas.models import LoginModel, UserRegisterModel
import requests
import datetime

router = APIRouter()

# 🔥 用户自助注册接口
@router.post("/api/register")
async def api_register(data: UserRegisterModel):
    try:
        # 1. 校验邀请码
        invite = query_db("SELECT * FROM invitations WHERE code = ?", (data.code,), one=True)
        if not invite:
            return JSONResponse(content={"status": "error", "message": "无效的邀请码"})
        
        if invite['used_count'] >= invite['max_uses']:
            return JSONResponse(content={"status": "error", "message": "邀请码已被使用"})

        # 2. 准备 Emby 连接
        host = cfg.get("emby_host"); key = cfg.get("emby_api_key")
        if not host or not key:
            return JSONResponse(content={"status": "error", "message": "系统未配置 Emby 连接"})

        # 3. 创建用户
        res = requests.post(f"{host}/emby/Users/New?api_key={key}", json={"Name": data.username})
        if res.status_code != 200:
            return JSONResponse(content={"status": "error", "message": f"用户名可能已存在"})
        
        new_id = res.json()['Id']

        # 4. 设置密码
        pwd_res = requests.post(f"{host}/emby/Users/{new_id}/Password?api_key={key}", json={"Id": new_id, "NewPw": data.password})
        if pwd_res.status_code not in [200, 204]:
            # 回滚：删除用户
            requests.delete(f"{host}/emby/Users/{new_id}?api_key={key}")
            return JSONResponse(content={"status": "error", "message": "密码设置失败"})

        # 5. 初始化策略 (启用账户)
        requests.post(f"{host}/emby/Users/{new_id}/Policy?api_key={key}", json={"IsDisabled": False, "LoginAttemptsBeforeLockout": -1})

        # 6. 计算过期时间
        expire_date = None
        if invite['days'] > 0:
            expire_dt = datetime.datetime.now() + datetime.timedelta(days=invite['days'])
            expire_date = expire_dt.strftime("%Y-%m-%d")
            # 写入用户元数据
            query_db("INSERT INTO users_meta (user_id, expire_date, created_at) VALUES (?, ?, ?)", 
                     (new_id, expire_date, datetime.datetime.now().isoformat()))

        # 7. 标记邀请码已用
        query_db("UPDATE invitations SET used_count = used_count + 1 WHERE code = ?", (data.code,))

        # 🔥 获取公网地址和欢迎语
        public_url = cfg.get("emby_public_url") or host # 如果没配公网，回退到内网地址
        welcome_msg = cfg.get("welcome_message") or "请妥善保管您的账号密码。"

        return JSONResponse(content={
            "status": "success",
            "server_url": public_url,
            "welcome_message": welcome_msg
        })

    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)})

@router.post("/api/login")
async def api_login(data: LoginModel, request: Request):
    try:
        host = cfg.get("emby_host")
        if not host: 
            return JSONResponse(content={"status": "error", "message": "请先在 config.yaml 配置 EMBY_HOST"})
            
        # 构造 Emby 认证请求
        url = f"{host}/emby/Users/AuthenticateByName"
        payload = {
            "Username": data.username, 
            "Pw": data.password
        }
        # 伪装成 Web 客户端
        headers = {
            "X-Emby-Authorization": 'MediaBrowser Client="EmbyPulse", Device="Web", DeviceId="EmbyPulse", Version="1.0.0"'
        }
        
        # 发送请求给 Emby Server
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        
        if res.status_code == 200:
            user_info = res.json().get("User", {})
            
            # 关键：检查是否为 Emby 管理员
            if not user_info.get("Policy", {}).get("IsAdministrator", False):
                return JSONResponse(content={"status": "error", "message": "权限不足：仅限 Emby 管理员登录"})
            
            # 登录成功：写入 Session
            request.session["user"] = {
                "id": user_info.get("Id"),
                "name": user_info.get("Name"),
                "is_admin": True,
                "server_id": res.json().get("ServerId") 
            }
            return JSONResponse(content={"status": "success"})
        
        elif res.status_code == 401:
            return JSONResponse(content={"status": "error", "message": "账号或密码错误"})
        else:
            return JSONResponse(content={"status": "error", "message": f"Emby 连接失败: {res.status_code}"})
            
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": f"登录异常: {str(e)}"})

@router.get("/logout")
async def api_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)