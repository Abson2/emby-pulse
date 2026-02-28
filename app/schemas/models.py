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
    # 🔥 新增：企业微信配置字段
    wecom_corpid: Optional[str] = ""
    wecom_corpsecret: Optional[str] = ""
    wecom_agentid: Optional[str] = ""

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
    # 彻底解决子文件夹同步的黑名单字段
    excluded_sub_folders: Optional[List[str]] = None

class NewUserModel(BaseModel):
    name: str
    password: Optional[str] = None 
    expire_date: Optional[str] = None
    template_user_id: Optional[str] = None 

class InviteGenModel(BaseModel):
    days: int 
    template_user_id: Optional[str] = None 
    # 支持批量生成的数量
    count: Optional[int] = 1

class UserRegisterModel(BaseModel):
    code: str
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
    # 🔥 新增：自定义客户端下载链接
    client_download_url: Optional[str] = ""