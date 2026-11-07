# -*- coding: utf-8 -*-
"""插件中心「开发资源」供应（2.5）：开发指南 / AI 开发提示词 / 示例插件。

内容以仓库 ``docs/PLUGIN_DEVELOPMENT.md`` 与 ``docs/PLUGIN_AI_PROMPT.md``
为唯一事实源，运行时读取并进程内缓存；Docker 镜像通过 Dockerfile 的
``COPY docs/`` 一并带入。示例插件（单文件 .py 与 zip 包）在内存中即时
生成，保证与当前版本的插件契约一致，不依赖任何静态资产。
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime

# ---- docs 定位：app/plugins/dev_resources.py -> 仓库根/docs ----
_DOCS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs")
)

_cache: dict = {}

EXAMPLE_PLUGIN_ID = "hello_plugin"
EXAMPLE_PLUGIN_VERSION = "1.0.0"


def _read_doc(filename: str) -> str:
    """读取 docs/ 下文档并缓存；读取失败返回空串（前端给出可读提示）。"""
    if filename in _cache:
        return _cache[filename]
    text = ""
    path = os.path.join(_DOCS_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as fp:
            text = fp.read()
    except OSError:
        text = ""
    _cache[filename] = text
    return text


def clear_doc_cache() -> None:
    """docs 内容更新后清缓存（测试与热更新用）。"""
    _cache.clear()


def get_dev_guide() -> str:
    """插件开发指南（docs/PLUGIN_DEVELOPMENT.md）。"""
    return _read_doc("PLUGIN_DEVELOPMENT.md")


def get_ai_prompt() -> str:
    """AI 开发提示词（docs/PLUGIN_AI_PROMPT.md）。"""
    return _read_doc("PLUGIN_AI_PROMPT.md")


# ---- 单文件示例插件 ----

EXAMPLE_PLUGIN_PY = '''\
"""单文件示例插件：最小可运行的 EmbyPulse 外部插件。

整个插件只有一个 plugin.py（外加必须的 manifest.json），
演示配置读取、日志与一个只读 API 端点。

打包导入：
    1. 把本文件与 manifest.json 放进同一个目录；
    2. 压成 zip（文件在压缩包根目录，不要再套一层文件夹）；
    3. 管理端 插件中心 -> 导入插件 -> 本地 ZIP 包 上传；
    4. 导入后默认禁用，确认代码无误再打开开关。
"""
from fastapi import Request
from app.plugins.base import PluginBase, require_admin


class HelloPlugin(PluginBase):
    id = "hello_plugin"
    name = "示例插件"
    description = "最小可运行示例：演示配置、日志与一个只读 API"
    icon = "fa-hand-wave"
    icon_color = "from-violet-500 to-purple-600"
    version = "1.0.0"
    author = "EmbyPulse"

    def __init__(self):
        super().__init__()
        self._setup_routes()

    def get_config_schema(self):
        return [
            {"key": "greeting", "label": "问候语", "type": "text", "default": "你好"},
        ]

    def on_enable(self):
        self.log("示例插件已启用")

    def on_disable(self):
        self.log("示例插件已停用", notify=False)

    def on_schedule(self):
        # 插件中心「定时执行」到点时触发；未配置间隔则不会被调用。
        self.log("定时任务执行了一次（示例）", notify=False)

    def _setup_routes(self):
        @self.router.get("/hello")
        async def hello(request: Request):
            require_admin(request)
            greeting = self._get_config().get("greeting", "你好")
            self.log("有人调用了 /hello")
            return {"status": "success", "message": greeting + "，来自示例插件"}


# 模块级实例：加载器优先识别这个变量
plugin = HelloPlugin()
'''

EXAMPLE_MANIFEST = {
    "manifest_version": 1,
    "id": EXAMPLE_PLUGIN_ID,
    "name": "示例插件",
    "version": EXAMPLE_PLUGIN_VERSION,
    "description": "最小可运行示例：演示配置、日志与一个只读 API",
    "author": "EmbyPulse",
    "icon": "fa-hand-wave",
    "icon_color": "from-violet-500 to-purple-600",
    # entry 为必填字段（2.3 校验强制），示例包入口即 plugin.py
    "entry": "plugin.py",
    "min_app_version": "2.3",
    "permissions": [],
    "source": {"type": "local"},
}

EXAMPLE_README = """\
# 示例插件（hello_plugin）

演示 EmbyPulse 外部插件的最小结构：

- `manifest.json` 插件元信息（必填字段见文件）
- `plugin.py` 入口文件，模块底部 `plugin = HelloPlugin()` 供加载器识别

导入：管理端 插件中心 → 导入插件 → 本地 ZIP 包。
导入后默认禁用；到插件卡片打开开关后，可用管理员令牌访问
`GET /api/plugins/hello_plugin/hello`（Bearer 鉴权见 OpenAPI 面板）。
"""


def build_example_zip() -> bytes:
    """在内存中即时生成示例插件 zip（无需静态资产，随当前契约走）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(EXAMPLE_MANIFEST, ensure_ascii=False, indent=2),
        )
        zf.writestr("plugin.py", EXAMPLE_PLUGIN_PY)
        zf.writestr("README.md", EXAMPLE_README)
    return buf.getvalue()
