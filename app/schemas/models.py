from pydantic import BaseModel
from typing import Optional, List

class LoginModel(BaseModel):
    username: str
    password: str

class SettingsModel(BaseModel):
    emby_host: str
    emby_api_key: str
    tmdb_api_key: Optional[str] = ""
    proxy_url: Optional[str] = ""
    webhook_token: Optional[str] = "embypulse" # 🔥 新增
    hidden_users: List[str] = []

class BotSettingsModel(BaseModel):
    tg_bot_token: str
    tg_chat_id: str
    enable_bot: bool
    enable_notify: bool
    enable_library_notify: Optional[bool] = False

class PushRequestModel(BaseModel):
    user_id: str
    period: str
    theme: str

class ScheduleRequestModel(BaseModel):
    user_id: str
    period: str
    theme: str

class UserUpdateModel(BaseModel):
    user_id: str
    password: Optional[str] = None
    is_disabled: Optional[bool] = None
    expire_date: Optional[str] = None 

class NewUserModel(BaseModel):
    name: str
    password: Optional[str] = None 
    expire_date: Optional[str] = None

# 🔥🔥 以下是本次新增的模型 🔥🔥

# 新增：生成邀请码参数
class InviteGenModel(BaseModel):
    days: int  # -1=永久, 1, 7, 30...

# 新增：用户注册参数
class UserRegisterModel(BaseModel):
    code: str
    username: str
    password: str