# EmbyPulse 插件开发规范（2.3）

> 适用版本：2.3 及以上。本文定义「外部插件」的包格式、运行时契约与安全边界。
> 想把功能需求直接交给 AI 生成插件？见 [给 AI 的话术指引](PLUGIN_AI_PROMPT.md)。
> 想 5 分钟跑通一个插件？先看 [插件开发 Quickstart](PLUGIN_QUICKSTART.md)。
> 只关心官方发布流程？见 [plugins/README.md](../plugins/README.md)。

## 1. 设计前提：导入 ≠ 启用

2.3 把「新增一个功能」从改主体代码变成开发一个插件包。因为插件包内含可执行 Python 代码，安全边界是硬性的：

| 阶段 | 做什么 | 是否执行插件代码 |
| --- | --- | --- |
| 导入（本地 zip / GitHub 拉取） | 校验包结构、manifest、路径、体积；落盘到数据卷；写 `origin.json` | **否** |
| 导入后 | 数据库写回 `enabled=false`，插件中心显示来源徽标 | 否 |
| 管理员显式启用 | 才 `import` 入口文件、实例化、注册路由 | 是 |
| 卸载 | 删除目录、清理启用状态 | 否（已加载代码需重启才彻底释放） |

因此：**写一个插件不需要改主体项目任何一行代码，也不需要重启才能看到新包**；但启用/卸载后建议重启，以彻底清理进程内已 import 的模块与动态注册的路由。

## 2. 五分钟快速开始

最小可运行示例在 [`examples/hello_plugin/`](../examples/hello_plugin/)：`manifest.json` + `plugin.py` 两个文件。

```bash
# 1) 复制示例
cp -r examples/hello_plugin examples/my_plugin
#    改 manifest.json 的 id / name / entry，改 plugin.py 里的同名类属性

# 2) 本地校验并打包（会打印 sha256 与 index.json 条目）
python tools/pack_plugin.py examples/my_plugin -o plugins/dist

# 3) 在管理端「插件中心 → 导入插件 → 本地 ZIP 包」上传产物
#    导入后默认禁用，手动打开开关即完成启用
```

`tools/pack_plugin.py` 复用服务端的 `validate_manifest` / `validate_zip`，打包阶段就会把不合规拦下来，不必等到上传报错。

## 3. 目录与包结构

```
my_plugin/
├── manifest.json     # 必填，包内根目录
├── plugin.py         # 入口，路径由 manifest.entry 指定
├── README.md         # 可选
└── assets/           # 可选，子目录允许
    └── logo.svg
```

- 所有路径都是**包内相对路径**，用 `/` 分隔，禁止 `..`、绝对路径、反斜杠、盘符、`://`、NUL。
- 不允许符号链接；目录项不允许携带内容。
- 允许扩展名：`.py .json .md .txt .html .css .js .svg .png .jpg .jpeg .webp .ico .yaml .yml .ini .cfg`。
- 隐藏文件 / 目录、`__pycache__`、`*.pyc` 会被打包工具自动跳过。
- 外部插件落在 `PLUGIN_EXTERNAL_DIR`（默认 `/workspace/data/plugins`，对应 compose 的 `./data` 卷），**不写进镜像内的 `app/plugins/`**，容器重建不会丢。

## 4. manifest.json 字段

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `manifest_version` | 是 | 固定 `1` |
| `id` | 是 | `^[a-z][a-z0-9_]{1,47}$`；不得与内置插件目录名或保留名（`base`、`__init__`、`plugin_dao`、`external`、`github_source`、`__pycache__`）冲突 |
| `name` | 是 | ≤ 80 字符 |
| `version` | 是 | ≤ 32 字符 |
| `entry` | 是 | 包内 `.py` 相对路径，文件必须真实存在于包内 |
| `description` | 否 | ≤ 300 字符 |
| `author` | 否 | ≤ 80 字符 |
| `icon` | 否 | FontAwesome class，仅字母/数字/空格/下划线/连字符，≤ 96 字符，默认 `fa-puzzle-piece` |
| `icon_color` | 否 | Tailwind 渐变 class，同上字符集，默认 `from-violet-500 to-purple-600` |
| `min_app_version` | 否 | ≤ 32 字符，供索引展示，当前不强制拦截 |
| `permissions` | 否 | 字符串数组，≤ 32 项，每项 ≤ 64 字符（声明式，非沙箱授权） |
| `source` | 否 | 对象，默认 `{"type": "local"}`；可含 `repo` / `ref` / `asset` / `path` |

**所有文本字段**都会拒绝 `< > & " ' ` \` 与控制字符，避免插件中心模板注入。描述里请写纯文本，不要带 HTML。

## 5. PluginBase 运行时契约

入口文件必须提供一个 `PluginBase` 实例。推荐模块级变量：

```python
from fastapi import Request
from app.plugins.base import PluginBase


class MyPlugin(PluginBase):
    id = "my_plugin"              # 必须与 manifest.id 逐字一致
    name = "我的插件"
    description = "一句话说明"
    icon = "fa-puzzle-piece"
    icon_color = "from-violet-500 to-purple-600"
    version = "1.0.0"
    author = "me"
    pro_only = False
    permissions = []

    def __init__(self):
        super().__init__()        # 创建 router / 初始化日志表 / 加载配置缓存
        self._setup_routes()

    def _setup_routes(self):
        @self.router.get("/ping")
        async def api_ping(request: Request):
            return {"status": "success", "data": {"pong": True}}


plugin = MyPlugin()
```

加载器先读模块级 `plugin`；若没有，则自动查找模块内的 `PluginBase` 子类并实例化。**实例化时 `id` 会与 manifest 逐字比对**，不一致直接拒绝加载。

可用的基类成员：

| 成员 | 说明 |
| --- | --- |
| `self.router` | `APIRouter`，前缀固定 `/api/plugins/{id}` |
| `self.enabled` | 只读属性，当前启用状态 |
| `self._get_config()` | 返回当前配置的副本（线程安全） |
| `self.has_permission(perm)` | 检查 `permissions` 声明 |
| `self.log(msg, level="info", notify=True)` | 写插件日志；`level` 支持 `info` / `warning` / `error` |
| `self.get_logs(limit=50)` / `self.clear_logs()` | 日志读取与清空 |
| `get_config_schema()` | 可覆盖，返回配置表单定义 |
| `get_page_url()` | 可覆盖，默认 `/plugins/{id}`，无独立页面可返回 `None` |
| `on_enable()` / `on_disable()` | 生命周期钩子，可覆盖 |
| `enable()` / `disable()` | 由加载器调用，不要覆盖 |

路由在 `__init__` 里用 `@self.router.get/post/...` 注册。`PluginBase` 已给 router 挂了启用守卫：插件禁用时其路由自动返回 404。

> 如果插件启动线程或定时任务，必须在 `on_disable()` 里停止并释放；否则即使卸载，线程仍可能留在进程内。

## 6. API 路由、鉴权与 OpenAPI

- 外部插件路由统一挂在 `/api/plugins/` 下，该前缀在全局 `AuthGateMiddleware` 中是**管理员专属**，因此插件 API 天然不对普通用户开放。不要指望用它承载用户端公开接口。
- 所有 `POST` / `PUT` / `DELETE` / `PATCH` 请求受 `CSRFMiddleware` 的 Origin / Referer 同源校验；浏览器同源 `fetch` 会自动携带，无需手工加头。
- 2.3 启用了 OpenAPI：`/openapi.json`、`/docs`、`/redoc` **默认仅管理员会话或管理员 API Token 可访问**，可用 `OPENAPI_PUBLIC=1` 显式开放。动态注册的插件路由也会出现在 schema 中，可在 `/docs` 直接调试。
- 文档页的内联脚本带有 CSP nonce，插件不要在响应里注入自己的 `<script>` 来改管理端页面。

## 7. 配置表单

`get_config_schema()` 返回字典数组，插件中心「配置」弹窗据此渲染并保存到 `plugin_state.config`。支持：

| `type` | 渲染 | 关键字段 |
| --- | --- | --- |
| `text`（或省略） | 单行输入 | `key`, `label`, `default`, `placeholder`, `hint`, `group` |
| `textarea` | 多行输入 | 同上 |
| `toggle` | 开关 | `key`, `label`, `default`（bool） |
| `select` | 下拉 | `options: [{value,label}]` |
| `multiselect` | 多选 | `options: [{value,label}]`，值为数组或逗号分隔字符串 |
| `action` | 不渲染输入框（主体自行处理自定义按钮） | — |

`key` 以 `_cron` 结尾时会渲染成 cron 选择器。`group` 相同项归到同一分组。读取时用 `self._get_config().get("key", 默认值)`，务必给默认值——用户可能从未打开过配置页。

## 8. 日志

`self.log()` 同时写插件日志表、输出容器日志，并可按 `notify_enabled` 配置触发全局通知。日志可在插件中心的「日志」按钮查看/清空。**高频事件不要逐条写日志**：只在状态变化、错误、任务开始/结束时记录，文案控制在一行内。

## 9. 安全边界（硬性，违反即拒绝导入）

| 约束 | 值 |
| --- | --- |
| zip 包大小 | ≤ 5 MiB |
| 解压后总大小 | ≤ 10 MiB |
| 单文件大小 | ≤ 2 MiB |
| 文件数量 | 1 ~ 200 |
| 单文件压缩比 | ≤ 200（防 zip bomb） |
| 禁止 | 路径穿越、绝对路径、反斜杠、盘符、URL 协议、NUL、符号链接、危险扩展名 |
| 禁止 | 覆盖 / 冒充内置插件 |
| GitHub 来源 | 仓库白名单（默认仅 `Abson2/emby-pulse`）、域名白名单、仅 HTTPS、逐跳重定向校验、条目强制 `sha256`、下载后逐字节校验 |

插件代码在宿主进程内以**完整权限**运行，没有沙箱。因此：

- 只从可信来源导入；导入后先审阅 `plugin.py` 再启用。
- 不要在插件里 `eval` / `exec` 外部输入、拼接 shell 命令、读写插件目录之外的文件。
- 不要引入新的第三方依赖（镜像内不保证存在），优先用标准库 + FastAPI + 项目已有的 `app.*` 模块。
- 不要读取或打印配置中的密钥；不要硬编码任何凭证。

## 10. 打包与发布到 GitHub 官方窗口

发布窗口是仓库内的 [`plugins/`](../plugins/README.md)：`plugins/index.json` 是索引，`plugins/dist/*.zip` 是产物。服务端只按「仓库 + ref + 相对路径」拼接下载地址，调用方无法传入任意 URL。

```bash
python tools/pack_plugin.py examples/my_plugin -o plugins/dist
# 把输出打印的 JSON 条目追加到 plugins/index.json 的 plugins 数组
# 提交并推送到 main
```

打包工具固定时间戳与文件顺序，同一份源码重复打包逐字节一致，`sha256` 可复现。升级请用**新版本号 + 新产物 + 新条目**，保留旧条目便于回滚；用户在导入时可指定 `ref` 安装特定分支/tag。

## 11. 调试与排障

| 现象 | 排查 |
| --- | --- |
| 上传后提示「已安装」 | 同 id 已存在；勾选「覆盖已安装的同 ID 插件」或先卸载 |
| 上传被拒，提示扩展名/路径/大小 | 用 `python tools/pack_plugin.py <dir>` 本地复现，工具会复用同一套校验 |
| 导入后卡片没有「配置 / 日志」 | 正常：未启用的外部插件尚未加载代码；打开开关后会自动刷新 |
| 开关打开又弹回 | 看容器日志中的「按需载入外部插件 xxx 失败」，通常是入口 import 报错或 `id` 与 manifest 不一致 |
| GitHub 索引为空 / 读取失败 | 索引固定读 `main` 的 `plugins/index.json`；确认已推送、或被 `PLUGIN_GITHUB_REF` 指到了别的分支 |
| 卸载后 API 仍可访问 | 动态注册的路由不会自动移除，重启应用即可彻底清理 |

## 12. 提交前自检清单

- [ ] `manifest.json` 五个必填字段齐全，`manifest_version=1`，`id` 合规且不与内置插件冲突
- [ ] 文本字段不含 `< > & " ' ` \` 与控制字符
- [ ] `entry` 指向的文件真实存在；模块提供 `PluginBase` 实例且 `id` 与 manifest 一致
- [ ] `__init__` 调用了 `super().__init__()`；线程/任务在 `on_disable()` 里清理
- [ ] 配置读取都带默认值；高频路径没有逐条写日志
- [ ] 只依赖标准库 + FastAPI + 项目已有模块
- [ ] `python tools/pack_plugin.py <dir>` 通过，产物 `sha256` 已写入索引
- [ ] 导入后确认默认禁用，再手动启用验证功能
