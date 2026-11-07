# 给 AI 的话术指引：用一句话需求生成 EmbyPulse 插件（2.3）

> 面向**不写代码的使用者**：把功能需求告诉 AI，让它按规范产出一个可直接导入的插件包。
> 本文的「主 Prompt」是自包含的，整段复制给任意 AI（本对话、ChatGPT、Claude 等）即可。
> 开发者细节见 [插件开发规范](PLUGIN_DEVELOPMENT.md)。

## 怎么用（3 步）

1. 复制下面【主 Prompt】整段，粘贴给 AI。
2. 在 Prompt 末尾的「我的需求」处写清你要什么（照着【需求填空模板】写更稳）。
3. 把 AI 输出的文件按它标注的路径存到本地目录，然后：

```bash
python tools/pack_plugin.py <插件目录> -o plugins/dist
```

打开管理端 **插件中心 → 导入插件 → 本地 ZIP 包** 上传生成的 zip。导入后**默认禁用**，确认无误再打开开关。

> 安全提醒：插件代码在宿主进程里拥有完整权限，没有沙箱。只启用你能读懂或信任其来源的插件；AI 生成的代码请先通读 `plugin.py` 再启用。

---

## 【主 Prompt】整段复制

```
你是 EmbyPulse 2.3 的外部插件生成器。请根据我最后给出的「我的需求」，
生成一个完整、可直接导入的插件包。不要向我追问可以通过合理默认值解决的问题；
确实缺少关键信息时，做出最小假设并在输出末尾用一行说明。

== 运行环境 ==
- EmbyPulse 2.3，FastAPI + Python 3.12+，插件在应用进程内运行。
- 插件包结构：目录内含 manifest.json 与入口 .py 文件（以及必要的辅助文件）。
- 插件代码只能依赖 Python 标准库、FastAPI、以及 EmbyPulse 项目已有的 app.* 模块。
  禁止引入任何新的第三方包，禁止执行 shell 命令、eval/exec 外部输入、访问插件目录之外的文件。

== 输出格式（严格遵守）==
- 只输出一个「文件清单」，随后按文件逐个给出完整内容。
- 每个文件用一个 fenced code block，并在代码块前单独一行写：文件路径: <相对路径>
- 不要在代码块外写实现讲解；不要输出 zip、不要输出需要我手动拼接的片段。
- 最后附一个「自检结果」小节，逐条勾对我下面列出的自检清单。

== 文件 1：manifest.json ==
必须是合法 JSON，字段如下（★为必填）：
  ★ "manifest_version": 1
  ★ "id": 只能小写字母开头，后接小写字母/数字/下划线，总长 2~48，
           且不得是内置插件名或 base/__init__/plugin_dao/external/github_source/__pycache__
  ★ "name": 纯文本，<=80 字符
  ★ "version": 纯文本，<=32 字符，如 "1.0.0"
  ★ "entry": 包内 .py 相对路径，如 "plugin.py"
    "description": 纯文本，<=300 字符
    "author": 纯文本，<=80 字符
    "icon": FontAwesome class，只允许字母/数字/空格/下划线/连字符
    "icon_color": Tailwind 渐变 class，同上字符集
    "min_app_version": "2.3"
    "permissions": 字符串数组，最多 32 项（仅声明用，不是权限沙箱）
    "source": {"type": "local"}
所有文本字段禁止出现 < > & " ' ` \ 以及控制字符；描述写纯文本，不要写 HTML。
包内文件扩展名只允许：.py .json .md .txt .html .css .js .svg .png .jpg .jpeg .webp .ico .yaml .yml .ini .cfg

== 文件 2：入口 .py 文件 ==
必须提供一个 PluginBase 实例，推荐模块级变量 plugin = XxxPlugin()。
骨架如下，不要发明骨架里不存在的 API（例如 self.db、self.background_task 都不存在）：

    from fastapi import Request
    from app.plugins.base import PluginBase

    class XxxPlugin(PluginBase):
        id = "<必须与 manifest.id 逐字一致>"
        name = "<与 manifest.name 一致>"
        description = "<与 manifest.description 一致>"
        icon = "<与 manifest.icon 一致>"
        icon_color = "<与 manifest.icon_color 一致>"
        version = "<与 manifest.version 一致>"
        author = "<与 manifest.author 一致>"
        pro_only = False
        permissions = []

        def __init__(self):
            super().__init__()   # 必须调用：创建 router / 日志表 / 配置缓存
            self._setup_routes()

        def _setup_routes(self):
            @self.router.get("/ping")
            async def api_ping(request: Request):
                return {"status": "success", "data": {"pong": True}}

    plugin = XxxPlugin()

可用基类成员（只能用这些，或标准 Python）：
  self.router                     APIRouter，前缀固定 /api/plugins/<id>
  self.enabled                    只读，当前启用状态
  self._get_config()              返回配置字典副本，读取时一律带默认值
  self.has_permission(perm)       检查 permissions 声明
  self.log(msg, level="info", notify=True)   写插件日志，level: info/warning/error
  self.get_logs(limit=50) / self.clear_logs()
  get_config_schema()             可覆盖，返回配置表单定义数组
  get_page_url()                  可覆盖，默认 /plugins/<id>，无页面可返回 None
  on_enable() / on_disable()      生命周期钩子，可覆盖

== 路由与鉴权硬约束 ==
- 路由用 @self.router.get("/xxx") / @self.router.post("/xxx") 在 _setup_routes() 里注册。
- /api/plugins/ 前缀是管理员专属；不要设计给普通用户访问的公开接口。
- 所有写操作（POST/DELETE 等）由浏览器同源 fetch 发起即可，系统有 CSRF 同源校验，
  不要自己加 token 头、不要试图绕过鉴权。
- 插件禁用时，基类守卫会让它的路由自动返回 404，不要自己实现开关判断。
- 不要在响应里注入 <script> 来改管理端页面（CSP 有 nonce 限制）。
- 外部网络请求如确有必要，必须把目标写死为常量并说明用途；禁止把用户输入直接当 URL。

== 配置表单（需要用户可调参数时才用）==
get_config_schema() 返回字典数组，每项可用字段：
  key, label, type, default, placeholder, hint, group, options
type 支持：text（默认）、textarea、toggle、select、multiselect、action（不渲染输入框）。
select / multiselect 需提供 options: [{"value": "...", "label": "..."}]。
读取配置一律 self._get_config().get("key", 默认值)，因为用户可能从未打开过配置页。

== 日志与后台任务 ==
- 只在状态变化、错误、任务开始/结束时写日志，高频事件不要逐条写。
- 日志一行内说清，不打印密钥、不打印完整请求体。
- 若启动线程或定时循环，必须在 on_disable() 里停止并释放，否则卸载后仍会残留。

== 明确禁止 ==
- 修改 EmbyPulse 主体代码、内置插件、数据库表结构或全局配置。
- 覆盖/冒充内置插件 id；读取或硬编码任何凭证。
- 引入新第三方依赖；假设某个宿主文件一定存在。
- 生成占位符、TODO、省略号或「此处省略」；每个文件都要完整可运行。
- 输出与需求无关的多余文件。

== 自检清单（在输出末尾逐条勾选）==
[ ] manifest.json 五个 ★ 字段齐全，manifest_version=1，id 合规且不与内置插件冲突
[ ] 所有文本字段不含 < > & " ' ` \ 与控制字符
[ ] entry 指向的文件确实在输出中，且提供 PluginBase 实例，实例 id 与 manifest.id 逐字一致
[ ] __init__ 调用了 super().__init__()；没有使用不存在的基类 API
[ ] 配置读取都带默认值；后台任务在 on_disable() 里清理
[ ] 只依赖标准库 + FastAPI + app.*；无 shell / eval / 任意 URL / 凭证
[ ] 每个文件都是完整内容，无占位符

== 我的需求 ==
（在这里写你的功能需求，可用下面的填空模板）
```

---

## 【需求填空模板】

把下面几行补完，粘到主 Prompt 的「我的需求」处：

```
插件要解决的问题：
触发方式：（手动点按钮 / 定时 / 每次某事件发生后 / 仅提供一个查询 API）
需要用户可配置的参数：
期望看到的结果：（返回什么数据 / 写什么日志 / 是否需要通知）
不涉及：（明确排除的范围，避免 AI 过度发挥）
```

示例：

```
插件要解决的问题：定期统计媒体库中缺少封面的条目数量，方便我决定要不要跑补全任务。
触发方式：手动调用一个只读 API，不自动定时。
需要用户可配置的参数：无。
期望看到的结果：返回一个 JSON，含总数和前 20 个条目名称；把本次查询写一行插件日志。
不涉及：不要真的修改媒体库，不要发通知，不要做定时循环。
```

---

## 【生成之后：三步验收】

1. **静态校验**（不需要启动服务）：

   ```bash
   python tools/pack_plugin.py <插件目录> -o plugins/dist
   ```

   这条命令会复用线上同一套 `validate_manifest` / `validate_zip`；不合规会在本机直接报错，并打印通过后的 `sha256`。

2. **通读代码**：重点看 `plugin.py` 里有没有网络请求、文件读写、shell 调用、密钥读取，以及 `id` 是否和 manifest 一致。

3. **小步启用**：插件中心导入 → 保持禁用 → 打开开关 → 在 `/docs`（仅管理员）里试调路由 → 看插件「日志」按钮。若开关弹回，看容器日志中的「按需载入外部插件 xxx 失败」。

---

## 【常见反例：这些话术会让 AI 产出不可用的插件】

| 别这么写 | 为什么 | 改成 |
| --- | --- | --- |
| 「帮我做个用户端页面」 | `/api/plugins/` 是管理员专属，且外部插件没有独立页面路由 | 「在插件中心提供配置项和一个只读 API」 |
| 「用 requests 调我的接口」 | 镜像不保证装 requests | 「用标准库 urllib.request，目标地址写成常量并说明用途」 |
| 「直接读宿主机配置文件」 | 外部插件不应访问自己目录之外的文件 | 「把需要的值做成配置项」 |
| 「顺便改一下主体的数据库」 | 插件不得改表结构，也会破坏升级兼容性 | 「只用 self.log() 记录，结果通过 API 返回」 |
| 「参考现有插件随便写」 | 容易让 AI 编造基类 API | 把本文【主 Prompt】整段贴给它，骨架已经给全 |

---

## 【进阶话术】

- **改已有插件**：把现有 `manifest.json` 和 `plugin.py` 全文贴给 AI，追加「只改 X，保持 id 与其余文件不变，输出改动后的完整文件」。
- **补配置表单**：「给现有插件增加 get_config_schema()，包含开关 A 和下拉 B，并在读取处提供默认值」。
- **排错**：把容器日志中的报错全文 + `plugin.py` 贴给 AI，追加「只解释根因并给出最小改动，不要重写整个插件」。
- **发布到官方窗口**：「按 plugins/README.md 的约定，给出需要追加到 index.json 的条目和打包命令」，然后照 [发布流程](../plugins/README.md) 操作。
