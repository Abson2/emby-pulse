# -*- coding: utf-8 -*-
"""EmbyPulse 2.3 最小外部插件示例。

本文件只在管理员于插件中心「显式启用」时才会被 import；导入 zip 包阶段
仅做校验与落盘，绝不执行本模块的任何一行代码。

演示能力：
  * 继承 PluginBase 声明元信息；
  * 用 self.router 注册一个只读 API（前缀固定为 /api/plugins/hello_plugin）；
  * 用 get_config_schema() 声明可在插件中心编辑的配置项；
  * 用 _get_config() 读配置、self.log() 写插件日志；
  * 用 on_enable() / on_disable() 做生命周期钩子。
"""
from fastapi import Request

from app.plugins.base import PluginBase


class HelloPlugin(PluginBase):
    # 类属性必须与 manifest.json 对应字段一致，尤其 id（加载器会逐字校验）
    id = "hello_plugin"
    name = "示例插件"
    description = "最小可运行示例：演示配置、日志与一个只读 API"
    icon = "fa-hand-wave"
    icon_color = "from-violet-500 to-purple-600"
    version = "1.0.0"
    author = "EmbyPulse"
    pro_only = False
    permissions = ["config:read"]

    def __init__(self):
        super().__init__()  # 创建 router、初始化日志表、加载配置缓存
        self._setup_routes()

    def _setup_routes(self):
        @self.router.get("/ping")
        async def api_ping(request: Request):
            """只读健康检查。插件被禁用时，基类的依赖会自动返回 404。"""
            greeting = self._get_config().get("greeting", "你好")
            return {
                "status": "success",
                "data": {
                    "pong": True,
                    "plugin": self.id,
                    "greeting": greeting,
                    "enabled": self.enabled,
                },
            }

    def get_config_schema(self):
        """插件中心的「配置」弹窗据此渲染表单。"""
        return [
            {
                "key": "greeting",
                "label": "问候语",
                "type": "text",
                "default": "你好",
                "placeholder": "将出现在 /ping 的返回值中",
                "hint": "仅作演示，不含任何副作用",
                "group": "基础设置",
            },
            {
                "key": "notify_enabled",
                "label": "日志触发全局通知",
                "type": "toggle",
                "default": True,
                "group": "基础设置",
            },
        ]

    def on_enable(self):
        self.log("示例插件已启用", notify=False)

    def on_disable(self):
        self.log("示例插件已停用", notify=False)


# 模块级实例：加载器优先读取它；也可省略，由加载器自动查找 PluginBase 子类
plugin = HelloPlugin()
