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
    webhook_token: Optional[str] = "embypulse"
    hidden_users: List[str] = []
    emby_public_url: Optional[str] = ""  
    welcome_message: Optional[str] = ""  

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
    enable_all_folders: Optional[bool] = None
    enabled_folders: Optional[List[str]] = None
    # 🔥 新增：支持子文件夹排除列表（黑名单）
    excluded_sub_folders: Optional[List[str]] = None

class NewUserModel(BaseModel):
    name: str
    password: Optional[str] = None 
    expire_date: Optional[str] = None
    template_user_id: Optional[str] = None 

class InviteGenModel(BaseModel):
    days: int 
    template_user_id: Optional[str] = None 

class UserRegisterModel(BaseModel):
    code: str
    username: str
    password: str