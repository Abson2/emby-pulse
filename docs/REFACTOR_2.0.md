# emby-pulse-hardened 2.0 — 架构梳理、重构与优化方案

> 目标版本：**2.0**（承接 v1.9.10）
> 硬性约束：**100% 保留全部现有功能、原有输入输出行为不变、不改动对外接口、不删除任何特性**
> 交付节奏：先梳理分析，再分批输出优化代码；每批独立可回滚、独立验证。

---

## 0. 结论先行

代码规模已达约 **15.4 万行**（Python 106,967 + 模板 41,137 + JS 5,691），是长期「追加功能 + 临时补丁」累积的结果，未做过同步重构。核心问题不是某段代码写错，而是**三个结构性债务**：

1. **装配层上帝对象**：`bot_service.py` 单点持有 40 个 `set_dependency_providers` 与 60+ import，43 个 service 全靠全局 provider 注入，全库 116 处同名注入函数。
2. **DDL 与请求路径混跑**：40 个 `ensure_*_tables()` 在 6 个公告端点等请求路径里每次执行 `CREATE TABLE/INDEX IF NOT EXISTS` + `PRAGMA table_info` + commit。
3. **数据访问每次开关连接**：`system_store.execute()` 每条 SQL 新建/关闭一次 SQLite 连接并 commit，`add_plugin_log()` 写一条日志要开 3 次连接。

本方案把工作分成 **批次 A【低风险清理】/ 批次 B【中风险抽离】/ 批次 C【大架构重构】**。本轮**只落地批次 A + 追加的用户级 Bark 乱码 Bug 修复（B0）+ 安全加固**，B/C 给方案不动手 —— 因为 A/B0 的每一项都能用纯标准库特征测试证明逻辑等价，而 B/C 需要引入应用级依赖与集成测试环境，风险不匹配「改动范围小、风险可控」的要求。

> 说明：下文性能判断为**静态代码分析推断**，未做生产环境 profiling；优化收益以「减少连接/DDL/SQL 扫描次数」这类可计数指标表述，不编造实测毫秒数。

---

## 1. 架构梳理

### 1.1 规模与分布

| 维度 | 数量 | 备注 |
| --- | --- | --- |
| Python 源码 | 106,967 行 | `app/` + `tests/` + `tools/`（含测试约 3.5 万行） |
| Jinja 模板 | 41,137 行 | 单文件最大 `plugins.html` 5,870 行 |
| 静态 JS/CSS | 5,691 行 | `request_app.js` 单文件 2,631 行 |
| 数据表 | 75 张 | `schema_registry.py` 集中登记，21 条索引 |
| `notification_bot` service | 43 个 | 40 个带 `set_dependency_providers` |
| 插件 | 15 个 | 单插件 `plugin.py` 普遍 700–1,767 行 |
| `ensure_*` DDL 函数 | 40 个 | 遍布 dao / 插件 / bot |
| `set_dependency_providers` | 116 处 | 全库同名函数定义 |

TOP 大文件：`app/plugins/hdhive/plugin.py` 1,767、`tg_whitelist_sync/plugin.py` 1,411、`user_backup/plugin.py` 1,375、`view_report/plugin.py` 1,169、`app/domains/users/auth.py` 1,218、`app/domains/notifications/messages.py` 1,115、`app/infra/db/schema_registry.py` 1,023、`app/bot/notification_bot/bot_service.py` 1,003。

### 1.2 模块划分（现状分层）

| 层 | 目录 | 职责 | 现状问题 |
| --- | --- | --- | --- |
| 装配层 | `app/bootstrap/`, `app/main.py` | 中间件注册、服务启停、10308 用户端 wrapper | 与 `bot_service` 的装配职责重叠 |
| 横切层 | `app/core/` | 配置单例 `cfg`、会话/CSRF/AuthGate/CSP 中间件、event_bus、安全工具 | `config.py` 393 行全局单例被所有层直接 import |
| 基础设施层 | `app/infra/` | `db/`（schema_registry、db_manager、system_store、各 dao）、`config/`（按域 settings 读取）、`clients/`（外部 API） | DDL 与业务 DAO 混放；`system_store` 无连接复用 |
| 领域层 | `app/domains/` | 12 个域：notifications / media_requests / users / playback / points / reports / risk / system / pwa / proxy / plugins | 与 `app/bot/`、`app/infra/` 双向引用 |
| 机器人层 | `app/bot/` | `notification_bot`（43 service + 编排）、`user_bot` | 最大的耦合枢纽 |
| 插件层 | `app/plugins/` | 15 个插件，各自 DAO + 路由 + 设置 + 日志 | 与核心同构重复 |
| 契约层 | `app/schemas/`, `app/shared/` | Pydantic 模型、版本号、视图上下文 | 相对健康 |
| 展示层 | `templates/`, `static/` | 服务端渲染 + 内联 JS + Alpine | 大量重复 JS 函数、无公共模块 |

### 1.3 关键依赖链路

```
Emby Webhook ──> app/domains/system/webhook.py
                    └─> webhook_events.py（事件名归一化 / 拆条 / 去重 / 忽略）
                          └─> event_bus (app/core/event_bus.py)
                                └─> NotificationBot.on_webhook_event
                                      └─> notification_bot_webhook_event_service
                                            └─> 入库队列 ──> handle_library_new_item/new_episode
                                                  ├─> notify_template 插件（模板渲染）
                                                  ├─> bark_push.safe_push（系统级 Bark）
                                                  ├─> user_notify_service（用户级广播）
                                                  └─> tg / wecom 出口

配置：app/core/config.cfg 单例 <── app/infra/config/*_settings.py <── 全域 + 插件
DDL：app/infra/db/schema_registry.py <── schema_bootstrap.ensure_registered_table <── 40 个 ensure_*()
装配：bot_service.py ──set_dependency_providers──> 40 个 service 的全局 provider 变量
```

### 1.4 耦合点清单

| ID | 耦合点 | 证据 | 影响 |
| --- | --- | --- | --- |
| C1 | `bot_service.py` 上帝装配器 | 1,003 行、约 60 条 import、40 次 `set_dependency_providers(...)`（107–376 行） | 改任一 service 依赖都要动这一个文件；导入期集中开销 |
| C2 | 全局 provider 注入样板 | 40/43 个 service 各自重复 `_xxx_provider = lambda: real_dep` + 逐参 `global`/`if ... is not None` 注入函数（每份 30–50 行），全库 116 处同名函数 | 纯为可测试性产生的重复代码；注入顺序敏感 |
| C3 | 全局配置单例 `cfg` | `app/core/config.py` 被 `infra/`、`domains/`、`bot/`、`plugins/` 直接 import；`infra/config/*_settings.py` 只是薄封装 | 无法替换实现、无法按请求隔离、测试需桩替换 `sys.modules` |
| C4 | 跨域反向依赖 | `domains/` 引用 `bot/`、`bot/` 引用 `domains/`；靠函数内惰性 import 规避循环 | 循环依赖隐患，静态分析困难 |
| C5 | DDL 与请求路径混跑 | `announcements_router.py` 6 个端点全部先调 `ensure_announcement_tables()`；全库 40 个 `ensure_*` | 每请求重复 DDL / PRAGMA / commit |
| C6 | 展示层无公共模块 | `escapeHtml` 在 9 个模板 + `dashboard/utils.js` 共 **10 处**重复定义；`parseMarkdown` 在 `register.html:269`、`settings.html:1607`、`request_app.js:2464` 三处且实现不一致 | 修一处漏九处；三版 `parseMarkdown` 行为不同（旧版转义 HTML、新版支持富文本） |
| C7 | 插件与核心同构 | 每个插件自带 `*_dao.py` + `ensure_*_tables()` + `_setup_routes()` + 设置/日志读写 | 15 份近似实现 |
| C8 | 巨型函数 | `_setup_routes`：`user_backup` 842 行、`library_cover` 687、`cover_generator` 450、`temp_account` 325；`bot_service` `start` / `user_bot_message_dispatcher.handle_message` 261 行 | 不可单测、改一处牵全身 |

### 1.5 冗余代码与坏味道

| ID | 冗余 | 证据 |
| --- | --- | --- |
| R1 | 同名函数重复定义 | `ensure_request_admin_messages_table` 在 `bot_service_dao.py:5` 与 `notification_bot_request_admin_message_sync_service.py:32` 各一份；`ensure_local_users_table` 在 `auth_dao.py:48` 与 `auth.py:285` 各一份；`_noop_audit_log` 6 份、`_check_user_exists` 6 份、`_default_get_plugin` 5 份 |
| R2 | 双胞胎 service | `notification_bot_library_new_item_service.py` 与 `..._new_episode_service.py` 逐行同构：`_default_get_plugin` / `_default_bark_push_provider` / `set_dependency_providers` / `_format_overview` / `_get_base_url` / `_render_caption` / `_get_platform` / `_bark_notify_*` 仅差剧集后缀逻辑 |
| R3 | 三套 HTML/Markdown 解析 | `register.html` 旧版（全转义）、`settings.html` 旧版、`request_app.js` 新版（富文本直通） |
| R4 | 10 份 `escapeHtml` | `tasks.html:318`、`history.html:715`、`users.html:2603`、`plugins.html:3481`、`messages.html:1003` + `:1722`（同文件两份！）、`settings.html:1433`、`dashboard/utils.js:84` 等 |
| R5 | 40 份 `ensure_*` DDL | 见 1.4 C5 |
| R6 | 40 份 provider 样板 | 见 1.4 C2 |
| R7 | 模板与 JS 的职责重叠 | 同一业务（公告预览提纯文本）在 `messages.html:1902`、`request_app.js:2450` 各写一遍正则 |
| R8 | 调试残留 | `config.py:223` 打印 `emby_host` 明文；`security_check.py:25` 把新生成 Token 的前 8 + 后 8 字符写入日志 |

**已存在的良性设计（不要动）**：`schema_registry.py` 集中登记 75 张表 + 21 索引；`security_utils.py` 的 `sanitize_html` / `sanitize_rich_html` / `sanitize_sql` 单点净化；`webhook_events.py` 纯函数模块（无 FastAPI 依赖，标准库可测）；`system_store` 明确的 DB 访问边界；CI 的 12 道纯标准库校验脚本。

### 1.6 性能瓶颈（静态推断，未做生产 profiling）

| ID | 瓶颈 | 量化（每次调用） | 优化方向 |
| --- | --- | --- | --- |
| P1 | `SystemStore.execute()` 每条 SQL 新建连接 | `system_store.py:41-46` 每次 `connect()` → `sqlite3.connect` → `commit` → `close` | 读多写少场景可复用连接；多语句操作合并为单连接单事务 |
| P2 | `add_plugin_log()` 3 次连接/事务 | `plugin_dao.py:60-83`：INSERT + 行数 DELETE + 时间 DELETE 各一次 `execute` | 合并为 1 次 `connect()` + 1 次 `commit()`，SQL 文本保持原样 |
| P3 | 行数裁剪 `NOT IN (SELECT ...)` | `plugin_dao.py:69-75`：单插件上限 500 行，每条日志都做子查询 + 全表比对 | 改为 `id < (SELECT MIN(id) FROM (... ORDER BY id DESC LIMIT ?))`，走主键范围删除 |
| P4 | 时间清理无索引 | `plugin_logs` 仅有 `idx_plugin_logs_plugin_id`；`created_at < ?` 需扫描该插件全部行 | 增补 `idx_plugin_logs_created_at`（`CREATE INDEX IF NOT EXISTS`，加法） |
| P5 | 每请求 DDL | `ensure_registered_table()` 对每个表：`CREATE TABLE` + `PRAGMA table_info` + N 条 `CREATE INDEX`；公告 6 个端点全调 | 启动时一次性 bootstrap + 进程内缓存（按 db_path 失效，提供 reset 钩子） |
| P6 | 冷启动集中开销 | `bot_service` 导入期 40 次注入 + `ThreadPoolExecutor` + 配置读取；4 个插件 `_setup_routes` 合计 2,304 行注册逻辑 | 延后到显式 `start()`，或按插件懒加载 |
| P7 | 前端单体 | `request_app.js` 2,631 行、`plugins.html` 5,870 行一次性下发解析 | 按 tab 拆分 + 按需加载（属 C 批） |

`report_service` / `stats_queries` 是否存 N+1 未逐条核实，**列为待验证项**，不在本轮下结论。

### 1.7 系统安全性审查

**已有防护（保持，不得削弱）**

| 防护 | 位置 |
| --- | --- |
| CSRF Origin 校验（含 Cloudflare Tunnel 兜底） | `app/core/csrf_middleware.py` |
| 默认拒绝鉴权网关 + 管理员路径前缀 | `app/core/auth_gate_middleware.py` |
| 10307 / 10308 双端口 Session Cookie 隔离 | `app/core/session.py`, `session_middleware.py`, `bootstrap/user_portal.py` |
| CSP nonce + `script-src-elem` + `frame-ancestors` + 安全响应头 | `app/core/security_headers_middleware.py` |
| SSRF 代理地址运行期二次校验与自动降级 | `app/utils/proxy_helper.py`, `url_validator.py` |
| 路径穿越防护（`..` / 分隔符 + realpath 前缀双重校验） | `app/domains/pwa/router.py`, `app/infra/db/db_manager.py` |
| 敏感字段 env 中央注册（强制覆盖 / 不落盘 / 掩码 / 只读 / 保存不覆盖） | `app/core/config.py::SENSITIVE_ENV_FIELDS` |
| 富文本标签白名单净化（零依赖） | `app/core/security_utils.py::sanitize_rich_html` |
| 凭证扫描 CI | `tools/check-no-secrets.py` |

**待加固项（按风险排序）**

| ID | 问题 | 证据 | 等级 | 本批 |
| --- | --- | --- | --- | --- |
| S1 | 用户端 `x-html` 渲染未净化富文本 | `templates/request.html:115` `welcomeModal.message`、`templates/request/tab_explore.html:82` 公告详情；`welcome_message` 保存时 `system/router.py:385` **未净化**，而 `register.html:393` 已用 DOMPurify | 中（存储型 XSS 面，管理员可控 / 历史脏数据） | ✅ |
| S2 | 日志泄露凭证片段 | `security_check.py:25` 输出新 Token 前 8 + 后 8 字符；`config.py:223` 输出 `emby_host` 明文 | 中低 | ✅ |
| S3 | 三版 `parseMarkdown` 行为不一致 | 旧版转义 HTML、新版直通，同一数据在不同页面呈现/安全语义不同 | 中 | 方案（B 批） |
| S4 | `script-src 'unsafe-inline' 'unsafe-eval'` | `security_headers_middleware.py:63-65`（Alpine 表达式所需） | 中（纵深防御缺口） | 方案（C 批，改动大） |
| S5 | `sanitize_rich_html` 允许 `span/div` 的 `style` 原值 | `security_utils.py` 属性白名单保留 `style`，未清洗 CSS 值（`url()` / `expression`） | 低（仅管理员可写） | 方案（B 批） |
| S6 | 表名 f-string 拼接 | `db_manager.py` / `audit_dao.py` / `session_dao.py` 等；**已核实均为内部常量或注册表白名单，用户输入全部走 `?` 占位符**，无注入 | 低（防御性） | 建议加断言 |

> **重要澄清**：SQL 拼接处不是注入漏洞。`audit_dao.py` 的 `where_clause` 只由固定条件串拼成，所有用户值经 `_escape_like` 转义后用 `?` 绑定；`db_manager.py` 的 `{table}` 来自内部表清单。本轮不做无谓改动，只在文档中标注为防御性建议。

---

## 2. 重构方案

### 2.1 等价性契约（每批都必须满足）

1. **不改对外接口**：路由路径/方法/参数/返回结构、模板变量名、JS 全局函数名、配置键名、数据库表结构，一律不变。新增的内部函数/常量只允许「加法」。
2. **不改合法输入输出**：对合法输入，函数返回值、DB 最终状态、页面渲染结果逐字节一致。安全加固只允许收紧**非法/恶意输入**的输出，不得改变合法内容。
3. **每批独立**：一批 = 一组改动 + 一组特征测试 + 一次全量回归 + 一个可回滚的提交。
4. **测试先行**：改动前先记录基线，改动后跑同一套测试；新增测试用纯标准库（CI 只跑 `python xxx.py`，无 pytest）。

### 2.2 批次 A【低风险清理】— 本轮落地

| ID | 改动 | 文件 | 等价性保证 |
| --- | --- | --- | --- |
| A1 | `add_plugin_log()` 三条语句合并为单连接单事务（**INSERT 与时间清理 SQL 逐字不变**） | `app/plugins/plugin_dao.py` | 表最终状态一致；函数仍返回 `None`；3 次连接/commit → 1 次 |
| A2 | 行数裁剪 `NOT IN` → 主键范围删除 `id < MIN(...)` | `app/plugins/plugin_dao.py` | 数学等价：保留最近 N 条，删除更旧的全部；双库逐行比对验证 |
| A3 | 增补 `idx_plugin_logs_created_at` 索引 | `app/infra/db/schema_registry.py` | `CREATE INDEX IF NOT EXISTS` 纯加法，不改表结构 |
| A4 | `ensure_announcement_tables()` 进程内一次性缓存（键 = `system_store.db_path`，提供 `reset_announcement_tables_cache()`） | `app/domains/notifications/message_dao.py` | DDL 幂等；换库路径自动失效；提供显式 reset 供测试/迁移 |
| A5 | 公告两处 GET 的解码循环抽公共 helper | `app/domains/notifications/announcements_router.py` | 纯内部去重，调用顺序与结果不变 |
| S1' | 用户端 `x-html` 统一走 `sanitizeRichText()`（DOMPurify 可用则净化，不可用降级原样） | `templates/request.html`, `request/tab_explore.html`, `static/js/request_app.js` | 合法 HTML 原样通过；CDN 失败时行为与现状完全一致 |
| S2' | 移除 Token 片段 / `emby_host` 明文日志 | `app/core/security_check.py`, `app/core/config.py` | 纯日志内容，无功能影响 |
| S3' | 抽公共 `_DANGEROUS_PATTERNS`（`sanitize_html` 与新增 `to_plain_text` 共用） | `app/core/security_utils.py` | 危险模式清单一致，行为不变 |
| **B0** | **修复用户级公告 Bark 推送 `&lt;p&gt;` 实体乱码** | `app/core/security_utils.py`, `app/domains/notifications/announcements_router.py` | 纯文本输入输出逐字不变；网页端富文本存储链路完全未动 |
| T1 | 新增纯标准库等价性测试 + 接入 CI | `tests/test_refactor_2_0_equivalence.py`, `.github/workflows/docker-publish.yml` | 70/0 全绿，逐项断言 A1–A5 / S1' / S2' / B0 |
| T2 | `verify_patches.py` 新增「问题 16」静态契约组 | `tools/verify_patches.py` | 锁定 2.0 改动、接口不变、CI 接入 |

### 2.2.1 追加 Bug 修复 B0（用户级公告 Bark 推送实体乱码）

用户反馈：公告网页端显示正常，但用户收到的 Bark 推送为 `&lt;p&gt;测试推送&lt;/p&gt;`。

- **根因**：`create_announcement()` 的用户级通知正文走 `sanitize_html()`，该函数内部 `html.escape()` 把 Quill 富文本 `<p>测试推送</p>` 转成 HTML 实体。Bark 是纯文本通道，既不渲染标签也不解码实体；`bark_push._strip_html()` 只剥离真实标签 `<...>`，对 `&lt;p&gt;`（由 `& l t ;` 等字符组成）无能为力。网页端是另一条链路（`sanitize_rich_html` 保留标签 + `x-html` 渲染），故显示正常。用户级 Bark 虽然复用 `bark_push.send()`，但内容在进入出口前就已被实体化 —— 所以必须在**内容源头**修，而不是再改出口。
- **修复**：新增 `to_plain_text(text, max_length)`（截断 → `html.unescape` → 剥离标签 → 危险模式清理），公告用户级通知的正文与标题改用它。兼容历史二次转义数据（`&lt;p&gt;` 也能还原）。
- **等价性**：普通纯文本输入输出与旧版逐字一致；含 `&`/`<`/`>` 的纯文本不再被错误实体化（这本身也是纯文本通道的乱码，属同一修复范围）；网页端存储仍用 `sanitize_rich_html`，显示链路零改动。
- **同类遗留（B 批）**：其余用户级通知（新片入库 / 心愿变动 / 管理员消息）的正文目前是模板渲染的**真实** HTML 标签，出口 `_strip_html` 能正常剥离，不产生实体乱码；建议 B 批在 `push_user_notify()` 层统一做纯文本化，形成单一收口。

### 2.3 批次 B【中风险抽离】— 给方案，暂不动手

| ID | 内容 | 风险点 | 前置条件 |
| --- | --- | --- | --- |
| B1 | 抽 `app/bot/notification_bot/_library_common.py`，合并两个 library_new service 的 7 个同构纯函数 | 两个模块的 provider 全局变量与 `bot_service` 注入点需逐一保持 | 先为每个 service 补完整边界测试（已有 2 个，需扩到覆盖所有分支） |
| B2 | 新建 `static/js/common.js` 统一 `escapeHtml` / `parseMarkdown`，10 个模板改为引用 | 会「删除」9 份重复定义（不删特性但要逐页验证渲染）；CSP nonce、加载顺序 | 每个模板补 DOM 级契约测试；逐页灰度 |
| B3 | 抽 `app/infra/db/ensure_once.py` 通用幂等缓存装饰器，40 个 `ensure_*` 逐个接入 | 部分 `ensure_*` 有运行时动态加列语义，缓存会破坏 | 先分类：纯建表 vs 带迁移；只迁前者 |
| B4 | `SystemStore` 增 `execute_many()` / 读写连接复用 | 连接生命周期、WAL 并发语义变化 | 压力测试 + 并发写测试 |
| B5 | 统一 `welcome_message` 写入侧净化 + 管理端回填保持原样 | `class="ql-align-*"` 等 Quill 属性在净化白名单外，会改变对齐格式 | 先扩展 `sanitize_rich_html` 允许 `ql-*` class |

### 2.4 批次 C【大架构重构】— 仅规划，不建议近期执行

| ID | 内容 | 为什么放最后 |
| --- | --- | --- |
| C1 | 用显式 DI 容器替换 116 处全局 provider，拆分 `bot_service.py` 为 `composition_root.py` + 43 个自注册 service | 牵动全部 service 与全部边界测试，一次改不完；必须与 B 批解耦 |
| C2 | 拆分 4 个 `_setup_routes`（842/687/450/325 行）为路由表 + handler 模块 | 插件路由行为敏感，需逐个插件做端到端回归 |
| C3 | 拆分 `request_app.js`（2,631 行）为按 tab 的 Alpine 组件；`plugins.html`（5,870 行）按插件拆 include | 前端缓存版本号、`x-data` 作用域历史踩过坑（见 v1.9.4/1.9.5 教训） |
| C4 | 引入统一 Repository 层，收敛 15 个插件各自 DAO | 需保持 75 张表结构完全不变 |
| C5 | 收紧 CSP 移除 `unsafe-inline` / `unsafe-eval` | Alpine / Tailwind CDN 依赖，需整体改造前端 |
| C6 | 配置单例改为请求作用域 / 显式注入 | 全库 import 面最广 |

---

## 3. 优化顺序与风险控制

推荐顺序（**先小后大、先可测后结构**）：

| 顺序 | 批次 | 改动范围 | 风险 | 验证手段 | 回滚 |
| --- | --- | --- | --- | --- | --- |
| 1 | 基线 | 0 行代码 | 无 | 跑现有 12 道 CI 校验 + 全量 `compileall` | — |
| 2 | **A + B0（本轮）** | 7 个 Python 源文件 + 3 个前端文件 + 1 个新测试 + 1 个 workflow + 版本/文档 | **低**：纯内部实现、SQL 等价、日志、前端净化降级、内容源头纯文本化 | 新增 `test_refactor_2_0_equivalence.py`（70/0）+ `verify_patches` 问题 16 组 + 现有全套回归 | 单 commit 直接 revert |
| 3 | B1/B3 | notification_bot / infra/db | 中 | 逐 service 边界测试 + 启动时 DDL 检查 | 分批 commit |
| 4 | B2/B4/B5 | 前端 + 存储层 | 中高 | DOM 契约测试 + 并发写测试 | 灰度 + 版本号回退 |
| 5 | C | 全库 | 高 | 需要完整集成环境（pytest + FastAPI 全依赖） | 分支级 |

**为什么 A 先做**：A 的每一项都满足「函数签名不变 + 合法输入输出不变 + 有纯标准库测试」，且 P2/P3/P5 是最高频路径（每次插件日志、每个公告请求），改动收益/风险比最优。C 批虽能根治耦合，但在当前「CI 只有标准库脚本、无 pytest 集成环境」的条件下无法证明等价，贸然执行违反硬约束。

---

## 4. 单元测试补充建议

### 4.1 现有测试盘点

`tests/` 下有约 60 个 boundary 测试（使用 pytest `monkeypatch` fixture），但 **CI 只跑 8 个纯标准库脚本**（`check-no-secrets` / `verify_patches` / `check_session_cookie_isolation` / `test_tg_whitelist_sync` / `test_bark_push` / `check_bark_channel_contract` / `test_webhook_events` / `test_user_notify` / `check_notification_bot_subscription` / `compileall` / YAML 解析）。本机 managed Python 无 `fastapi` / `sqlalchemy` / `pytest`，因此**新增验证必须写成可直接 `python tests/xxx.py` 运行的纯标准库脚本**，否则等于没测。

### 4.2 建议补充的特征测试（Golden / Characterization）

| 测试 | 锁定什么 | 形式 |
| --- | --- | --- |
| `test_refactor_2_0_equivalence.py`（本轮新增，**70/0 全绿**） | A1–A5 / S1' / S2' / B0 的全部行为：日志截断/行数/留存三语义、SQL 等价性（内存 SQLite 双库逐行比对，边界 N=1/2/7/500 + 多插件隔离）、DDL 缓存次数与换库失效与异常重试、解码 helper、前端净化降级、日志不含凭证、`to_plain_text` 纯文本等价与实体乱码修复（含端到端经 Bark `_strip_html`） | 纯标准库 + `sqlite3` 内存库，桩替换 `system_store` + AST 抽取执行 |
| 配置单例契约测试 | `SENSITIVE_ENV_FIELDS` 的 env 覆盖 / 掩码 / 只读语义在重构后不变 | 已有 `verify_patches` 覆盖，补强即可 |
| 通知链路快照测试 | `handle_library_new_item/new_episode` 在给定 item 下产出的 caption / payload 逐字节快照 | B1 前必须补齐（现仅覆盖部分分支） |
| 模板契约测试 | 10 个 `escapeHtml` / 3 个 `parseMarkdown` 收敛前后输出一致 | B2 前补齐；用 Node 抽取内联 JS 跑字符串断言 |
| DDL 幂等测试 | 40 个 `ensure_*` 连续执行两次结果一致、缓存接入后表结构一致 | B3 前补齐 |

### 4.3 CI 接入

在 `.github/workflows/docker-publish.yml` 的 `verify` job 增加一步 `python tests/test_refactor_2_0_equivalence.py`，位置放在 `verify_patches.py` 之后。所有新增测试保持零第三方依赖。

---

## 5. 2.0 版本同步清单

| 位置 | 改动 |
| --- | --- |
| `app/shared/version.py` | `APP_VERSION = "2.0"` |
| `Dockerfile` | `ENV APP_VERSION=2.0-hardened` |
| `docker-compose.yml` | 默认 `IMAGE_TAG:-2.0`，已发布列表追加 `2.0`（**保留 1.9.10 可回退**） |
| `templates/request.html` | `request_app.js?v=2.0`（每次发版必改，含本轮前端改动） |
| `CHANGELOG.md` | 新增 `## 2.0` 段（架构优化 / 性能 / 安全 / 等价性验证） |
| `README.md` | 版本锁定表述、架构说明链接 |
| `tools/verify_patches.py` | 版本断言 → 2.0，CHANGELOG 断言追加 `## 2.0` |
| `docs/REFACTOR_2.0.md` | 本文档 |

> `2.0.1` 补丁版按同一清单同步为 `2.0.1`（`APP_VERSION=2.0.1-hardened` / `IMAGE_TAG:-2.0.1` / `request_app.js?v=2.0.1` / `## 2.0.1` / README 主页收敛），`v2.0` tag 保持不动。
> 第 8 节主线优化批次在推进期间**不改版本号、不发版**，只推进架构优化本身；主线收口后按同一清单统一发布 `2.0.2`（`APP_VERSION=2.0.2-hardened` / `IMAGE_TAG:-2.0.2` / `request_app.js?v=2.0.2` / `## 2.0.2`），`v2.0` / `v2.0.1` tag 均不重推。本次为零功能变化，主页功能清单与使用说明不变，仅同步版本锁定。

**兼容性**：所有旧版本标签（`v1.9.10` 等）与 `IMAGE_TAG` 回退路径保持不变；对外 API、模板变量、配置键、DB schema 均不改。

---

## 6. 后续批次排队

本轮交付 = 「完整分析 + 批次 A 代码 + B0 乱码修复 + 安全加固 + 等价性测试 + 2.0 版本号 + 全量回归」。
下一轮建议按 `B1（library_common 抽离）→ B3（ensure_once）→ B2（前端公共 JS）` 推进，每轮单独提交。
C 批建议等 CI 具备 pytest + FastAPI 集成环境后再启动。

> **修订（第二批）**：此处原建议的「B0 收口（push_user_notify 层统一纯文本化）」经复核**不采纳**。统一化会同时改变多个入口对模板真实 HTML 标签与 `&` / `<` / `>` 的输出，而 CI 只有纯标准库、无法证明全入口逐字节等价，与「100% 行为不变」硬约束冲突。实际改为逐入口只修确凿 bug，见第 7 节。

---

## 7. 第二批零风险加固（v2.0 tag 之后）

### 7.1 筛选标准

用户标准：「风险最小、稳定性、安全性、100% 功能保留」。据此**只纳入三类改动**：

1. 与已确认 bug 完全同源、可单点修复且不触碰其它链路；
2. 纯加法、零运行时行为变更的静态契约与回归测试；
3. 修复测试自身的误报。

凡涉及运行时行为、公共模块抽取、依赖注入、DDL 通用化、前端公共 JS 收敛，一律排除，仍留在 B/C 批排队。

### 7.2 全入口复核结论（先排查再动手）

以 `_uns.notify_users_by_flag` / `_uns.push_user_notify` / `_uns.notify_request_users` 为索引，逐条判断用户级通知正文来源：

| 入口 | 正文来源 | 是否存在同类实体乱码 | 本批动作 |
| --- | --- | --- | --- |
| `announcements_router.py::create_announcement` | Quill 富文本 | 有（首批 B0 已修） | 保持 `to_plain_text` |
| `messages.py::send_message` | 管理员输入纯文本，经 `sanitize_html()` 实体化 | **确凿同类**：`&` / `<` / `>` 变 `&amp;` / `&lt;` / `&gt;` | 改为 `to_plain_text` |
| `notification_bot_library_new_item_service.py` | 模板渲染的真实 `<b>` 标签 | 无，Bark 出口 `_strip_html()` 可剥离真实标签 | 不动 |
| `notification_bot_library_new_episode_service.py` | 同上 | 同上 | 不动 |
| `management_router.py` 心愿状态变动 | 状态文案 | 未见实体化链路 | 不动 |

判断依据：Bark 是纯文本通道，既不渲染标签也不解码实体。`_strip_html()` 只能剥离 `<...>` 真实标签；实体乱码只能由内容源头的 `html.escape()` 产生。因此修复点必须落在「实体化发生处」，而不是出口层或统一中间层。

### 7.3 落地清单

| 文件 | 改动 | 运行时行为影响 |
| --- | --- | --- |
| `app/domains/notifications/messages.py` | `push_user_notify()` 正文 `sanitize_html` → `to_plain_text`，导入追加一个符号 | 仅管理员消息的用户级 Bark / Webhook 正文；网页端 `insert_admin_message()` 存储与显示链路一行未动，参数 / `flag_key` / 异常吞噬不变 |
| `tools/check_security_contracts.py`（新增） | 10 组静态 + 行为级安全契约断言，纯标准库只读源码 | 无（CI 脚本，不进镜像运行路径） |
| `.github/workflows/docker-publish.yml` | `verify` job 增加安全契约 + 富文本净化回归两步 | 仅 CI |
| `tests/test_refactor_2_0_equivalence.py` | 扩展第 8 节锁定第二批边界；把「只用标准库」断言改为 AST import 白名单 | 仅测试 |
| `tools/verify_patches.py` | 新增「问题 17」边界组与「问题 18」主页文档收敛组 | 仅 CI |
| `README.md` | 收敛为功能清单 + 使用说明（357 → 185 行），保留 4 段式，删除按版本罗列的修复史 | 无 |
| `CHANGELOG.md` / 本文档 | 记录第二批决策、`v2.0.1` 版本点与主页文档约定 | 无 |

**测试断言误报修复**：等价性测试原用裸子串 `"import bleach" not in sec_contract`，而安全契约脚本自身第 129 行就含该字符串（用于检查 `security_utils.py` 未被污染），造成自匹配误伤（87/1）。改为 AST 解析真实 import 语句、只检查顶层模块名白名单，既排除误报又比子串更严谨（88/0）。教训与 2.0 首批一致：**静态断言不得用裸子串判断「不存在」**。

### 7.4 刻意不做（及理由）

| 候选项 | 不做的理由 |
| --- | --- |
| `push_user_notify` 层统一纯文本化（第 6 节原建议，已撤销） | 会同时改变 4 个入口对模板 HTML 与特殊字符的输出；CI 无 pytest / FastAPI 集成环境，无法证明全入口逐字节等价，违反「行为不变」 |
| DI 容器 / `service_registry` 改造（116 处 `set_dependency_providers`） | 启动顺序与依赖注入时序属运行时行为，是 C 批范畴 |
| 前端公共 JS 收敛（10 处 `escapeHtml` / 3 处 `parseMarkdown`） | 三份 `parseMarkdown` 行为不一致，收敛必然改变部分页面输出，需 Node 快照测试先行（B2） |
| `ensure_once` 通用 DDL 缓存 | 当前仅公告表做进程内缓存；通用化需逐个验证 40 个 `ensure_*` 的幂等性与异常重试语义（B3） |
| B1 / B2 / B3 / C 批 | 维持排队，等对应特征测试补齐后再动 |

### 7.5 回归矩阵

| 脚本 | 覆盖 | 实测结果 |
| --- | --- | --- |
| `tests/test_refactor_2_0_equivalence.py` | A 批等价性 + B0 + 第二批边界 | 88/0 |
| `tools/check_security_contracts.py` | 10 类安全契约（含行为级净化） | 42/0 |
| `tools/verify_patches.py` | 全量静态断言（204 → 224：问题 17 组 16 条 + 问题 18 主页收敛组 4 条） | 224/0 |
| `tests/test_sanitize_rich_html.py` | 富文本净化 / 历史二次转义兼容 | 14/0 |
| `tools/check_bark_channel_contract.py` | Bark 前后端字段契约 | 51/0 |
| `tests/test_bark_push.py` | Bark 出口 HTML 剥离 / Webhook token / env 掩码 | 103/0 |
| `tests/test_webhook_events.py` | Emby 事件映射 / 拆条 / 忽略门控 | 58/0 |
| `tests/test_tg_whitelist_sync.py` | 求片同步插件推送与删单联动 | 102/0 |
| `tests/test_user_notify.py` | 用户端口通知设置与用户级推送 | 12/0 |
| `tools/check_session_cookie_isolation.py` | 10307/10308 Cookie 隔离 | 15/0 |
| `tools/check_notification_bot_subscription.py` | 仅启用 Bark 时的入库事件订阅 | 29/0 |
| `tools/check-no-secrets.py` | 凭证 / 个人配置扫描 | 606 文件 / 0 findings |
| `compileall -q app/` / `node --check static/js/request_app.js` / YAML 解析 / `git diff --check` | 语法 / 前端 / compose+workflow / 空白 | 全绿（verify job 16 步） |

### 7.6 版本策略

`v2.0` 已发布、tag 与 GHCR 镜像均已推送。本批先作为 `main` 上的收尾提交（`2a94e83`），随后发布 **`v2.0.1` 零风险补丁版**：按第 5 节清单把 6 个版本点从 `2.0` 同步到 `2.0.1`（含 `request_app.js?v=2.0.1` 缓存破坏），并同步 `verify_patches.py` / 等价性测试的版本断言。**`v2.0` tag 不重推**（重推会让已部署实例、Release 与镜像摘要漂移），`IMAGE_TAG=1.9.10` 回退路径不变。

同时按用户要求把 **GitHub 主页 README 收敛**为「功能清单 + 使用说明」：保留原 4 段式结构，删除按版本罗列的修复史与各版本根因 / 症状说明，详细更新说明一律指向 [CHANGELOG](../CHANGELOG.md) 对应版本与 GitHub Releases；`verify_patches.py` 新增「问题 18」组把该约定固化为 CI 断言。

---

## 8. 主线优化第三批（v2.0.1 之后：架构优化本体）

方向校准：主线是「把项目优化成 2.0」——消除重复、抽离公共逻辑、解耦、提升效率；补丁发版 / 主页文档 / CI 确认 / 任务状态清理均为支线。本批**只动优化本体**：不改版本号、不改 README、不打 tag、不改对外接口，每批独立可 revert。

### 8.1 本批落地

| 批次 | 改动 | 等价性证明 |
| --- | --- | --- |
| **B1** | 新建 `app/bot/notification_bot/_library_common.py`；两个双胞胎 service 的 provider 装配样板（48 行 → 15 行）与 `_format_overview` / `_format_type` / `_get_base_url` / `_get_platform` 四个同构纯函数收敛为薄包装。两文件 291+329 → 249+293 行，**净减 78 行重复** | `tests/test_b1_library_common_equivalence.py` 53/0：签名 11 个 keyword-only 参数不变、逐项覆盖 / None 不覆盖、**两模块 provider 状态互不串扰**、纯函数与旧实现逐字复制的 oracle 多组输入一致、后半段 handler / Bark / 用户级广播静态契约完整 |
| **B3** | 新建 `app/infra/db/ensure_once.py` 通用门控（按 key 缓存 / 异常不写缓存 / reset / 线程锁）；`message_dao.ensure_announcement_tables()` 迁入，**保留** `_announcement_tables_ready` / `_announcement_tables_lock` / `reset_announcement_tables_cache()` 全部对外符号 | 既有 2.0 等价性测试中 DDL 连接次数 / 换库失效 / 异常重试 / reset 断言全部保持 88/0；B 批脚本另测门控本身 |
| **B3-B** | 全库 40 个顶层 `ensure_*` 完整分类，严格 AST 匹配出 **20 个逐字同构的纯建表样板**（18 个文件），新建无状态 `app/infra/db/ddl_bootstrap.py`，调用点收敛为一行 `ensure_registered_tables(system_store, ensure_registered_table, (...))`；18 个 DAO 文件 `+38 / -86`，**净减 48 行重复**。`store` / `ensure_fn` 显式传入，helper 不持有任何 `system_store` / 注册表状态，**不引入缓存**，带 PRAGMA / ALTER / 业务 SQL 的迁移型 `ensure_*` 一律不接入 | `tests/test_b_batch_equivalence.py` 第 5 节（37/0）：以「抽离前逐字复制的旧骨架」作 oracle，对 1 / 3 / 0 张表逐事件比对 connect → cursor → DDL 顺序 → commit，并验证 DDL 异常同样外抛且不 commit；静态契约用 AST 真实调用计数锁定 20 个接入点、迁移型未改、helper 无状态。`verify_patches.py` 问题 20 组 238/0 |
| **B4** | `SystemStore.execute_many()` 纯加法：多条写语句共用 1 连接 1 事务，失败整批回滚；`execute()` 逐条提交语义**一行未动** | 内存 SQLite 验证两条语句 1 次连接、失败批次零残留、`execute()` 源码仍为原实现 |
| **S5** | `sanitize_rich_html` 对 `style` 清洗 `url()` / `expression()` / `@import` / `behavior:` / `-moz-binding`，合法声明逐字不变（只收紧非法输入，符合等价性契约第 2 条） | 合法 `style` 输出比对、4 类危险值剥离、script / onerror / javascript: 与 `<p>欢迎大家</p>` 回归全绿 |
| **S6 + 测试** | 静态断言「表名 f-string 走集中注册表」；新增两个纯标准库回归、CI 接入 2 步、`verify_patches.py` 问题 19 组、安全契约 S5/S6 断言 | 全套 16 项回归 0 失败 |
| 兼容修复 | `check_notification_bot_subscription.py` 的 AST 抽取命名空间补入 `_library_common` **真实实现**（该脚本忽略 import 语句） | 既有订阅链路 29/0 恢复全绿 |

**关键设计**：B1 的公共模块**不持有任何 provider 状态**，`apply_dependency_providers(globals(), ...)` 只写调用方命名空间，因此 `bot_service.py` 分别给两个 service 注入的桩仍各自独立；这是双胞胎 service 能安全抽离的前提。

### 8.2 剩余项与客观约束（不编造完成）

| 项 | 状态 | 为什么本批不动 |
| --- | --- | --- |
| **B2 前端公共 JS** | 前置证据已实测 | 8 处 `escapeHtml` 实测为 **4 类不同实现**：DOM `textContent`、DOM `innerText`、正则 `&#039;`、正则 `&#39;`，且对 falsy 值 / 非字符串输入处理不同；三版 `parseMarkdown` 也不一致。**删除或统一定义必然改变页面输出**，与「合法输入输出逐字不变」直接冲突。需 DOM 级契约测试逐页灰度，纯标准库 CI 做不到 |
| **B5 写入侧统一净化** | 未动 | `class="ql-align-*"` 等 Quill 属性在净化白名单外，会改变公告对齐格式（合法输入被改） |
| **B3 余下 `ensure_*`** | 已分类，未动 | 40 个顶层 `ensure_*` 已全量分类：20 个纯建表样板由 B3-B 抽离，其余含 PRAGMA 动态加列 / `ALTER TABLE` / 业务 SQL / 非同构变体，套 helper 或缓存会改变 DDL 时序与异常语义，**保持原样**；后续如再抽离须先逐个证明同构 |
| **P1 连接复用（B4 完全版）** | 部分 | 本批只加 `execute_many()`；把既有 `execute()` 改成复用连接会改变 WAL 并发与事务边界，需并发写压测 |
| **P6 / P7 / C1–C6** | 未动 | C5 移除 `unsafe-eval` 会让 Alpine 表达式直接失效（**功能破坏**）；C1 牵动 116 处 provider 与全部边界测试；C3 拆分 `request_app.js` 历史上踩过 `x-data` 作用域与 JS 缓存坑。均需 pytest + FastAPI 集成环境逐字节证明等价 |
| `report_service` / `stats_queries` N+1 | 待核查 | 静态分析未逐条核实，不做无依据结论 |

### 8.3 完成度口径

- **A 批低风险清理：100%**。
- **B 批**：B1 完成、B3 完成「通用门控 + 首个接入」、B3-B 完成「20 个纯建表样板抽离」、B4 完成纯加法部分、S5 完成；B2 / B5 / B3 余下迁移型因等价性无法证明而按硬约束暂停。
- **C 批：0%，仅规划**。这不是遗漏，而是「100% 功能保留」在纯标准库 CI 条件下的必然边界；强行执行会产出无法证明等价的全库改动。
- 所有落地项均按「先建基线 → 改动 → 纯标准库等价性验证 → 全量回归 → 独立提交」推进，可单独 revert。

### 8.4 本批教训：静态断言必须判定「真实调用」，不能用裸子串

B3-B 的调用点计数最初写成 `"ensure_registered_tables(" in src`，结果把 `ddl_bootstrap.py` 里 **helper 自身的定义行** `def ensure_registered_tables(...)` 也算成调用点，得到 21（真实为 20），并连带让「签名不变」断言误判。这与第二批 `tests/test_refactor_2_0_equivalence.py` 的 87/1 误报同源：检查脚本自身的字符串 / 定义行会被裸子串命中。**约定**：判断某调用「存在 / 不存在」时，一律用 `ast.Call` + `ast.Name` 精确判定函数体内真实调用，或用精确正则锚定，不用裸子串；`tests/test_b_batch_equivalence.py` 第 5 节与 `verify_patches.py` 问题 20 组均已按此实现。

### 8.5 剩余项要在 100% 保留功能下推进，各自先补什么

先把「等价」定义成可机器判定：对同一输入集，新旧实现的**可观察结果逐字节相同**，且 golden **必须在改代码前采集**（先建基线）。分层：SQL 层 = `sqlite_master` + 数据行 + SQL 事件序列 + 异常时机；JS 层 = DOM 输出；HTTP 层 = 状态码 / 响应体 / 头 + DB 终态；副作用层 = 日志 / 通知 payload。

| 项 | 必须补齐的前提 | 结论 |
| --- | --- | --- |
| 迁移型 `ensure_*` | 内存 SQLite 双库 oracle，按「空库 / 半迁移库 / 全量库」三种初态跑两轮比 schema + 数据行 + 事件序列；**纯标准库即可，零新依赖** | **可继续做**：21 个剩余里 `ensure_announcement_tables` / `ensure_calendar_notify_config_table` / `ensure_users_meta_column` 仍是 `connect+注册表DDL` 骨架，可拆「helper + 保留附加语句」并复用 B3-B 事件序列 oracle；126/77/33/19 行的 4 个大型迁移（PRAGMA/ALTER/数据迁移）只补幂等契约、不抽公共逻辑、不接缓存；另 10 个「其它」（建目录 / env 管理员 / token）不是 DDL，不进批次 |
| B2 前端公共 JS | Node + jsdom/linkedom，8 处对 falsy / 非字符串 / 引号实体 / 换行 / Unicode 输入矩阵逐页采 golden | **统一与硬约束互斥**：实测 8 处为正则 `&#039;`（+falsy 检查）、`(unsafe\|\|'').toString()` + 正则、DOM `textContent` 有/无 falsy 检查、DOM `innerText`、`&#39;` 等 4 类行为；统一后至少 3 类输出必然变化。终点是「按页保留 + 契约锁」（已做），或明确授权输出变更后按功能变更发版 + 逐页灰度 |
| B5 写入侧统一净化 | 枚举 Quill 产出 HTML 全集（对齐 / 缩进 / 列表 / 颜色 / 字号 / 链接 / 图片）经 `sanitize_rich_html` 的输出固化为 golden；任何统一使 golden 变化即否决 | `_RICH_ALLOWED_ATTRS` 中 `span`/`div` 只允许 `style`，`class` 不在白名单，统一会剥离 `ql-align-*`；且「网页存储走富文本净化 / 用户级纯文本通知走 `to_plain_text`」是修实体乱码时确立的隔离边界，不是冗余 |
| C 批大重构 | CI 具备 pytest + FastAPI + 全依赖；建四层快照（API 响应 / DB 全量 dump / 模板渲染 HTML / 116 处 provider 装配状态），每个拆分单独分支逐字节比对零差异才合并 | 属工程环境建设，需另起一轮；C5 移除 `unsafe-eval` 会让 Alpine 表达式失效，属功能破坏，必须排除或先重写表达式 |

**口径**：第 ① 项是 `2.0.2` 之后可低风险推进的增量；②③ 的「完成」是确认不该做；④ 是环境建设。三者都不构成本版欠账。第 8 节全部落地项已随 `v2.0.2` 发布。

---

### 8.6 梯队 0：补齐验证与治理能力（v2.0.2 之后，零运行时代码改动）

**方向切换**：2.0 主线中「可证明等价」的重构已按 8.3 收口。继续往运行时代码里拧优化，边际收益下降而风险上升；真正的缺口在**验证网**——CI verify 只显式接入 14 个脚本，而仓库里 171 个 `tests/*.py` + `tools/*.py` 中有 142 个在纯标准库环境下可直接运行，128 个长期闲置。本批**一行运行时代码都不改**，只新增校验、测试、基线数据与文档。

**硬约束**：`app/**` 零改动；不改版本号、README、tag；不发版；不改 CHANGELOG 已发布版本；先建基线 → 落地 → 全量回归 → 独立提交，可单独 revert。

#### 8.6.1 基线探查（改代码前采集）

| 探查 | 方法 | 实测 |
| --- | --- | --- |
| 全量脚本可跑性 | 171 个脚本逐个 `subprocess`，cwd=ROOT、40s 超时、纯标准库环境 | 142 通过 / 29 失败；29 个**全部**是缺第三方依赖（pytest / anyio / PIL / fastapi 应用配置），**0 个断言失败** |
| 三条净化通道 | 25 组输入 × 3 通道，记录一次/二次调用输出与结构特征 | golden 固化到 `tests/sanitizer_golden.json`（25×3） |
| 架构指标 | AST + 正则只读静态分析，口径见脚本 docstring | 见 8.6.3 基线列 |

**三条反直觉事实（必须写进契约，不能按常识写测试）**：

1. `sanitize_html` **不幂等**（25 组中 18 组非幂等）：`html.escape` 会对 `&` 二次转义，`<p>测试推送</p>` → `&lt;p&gt;…&lt;/p&gt;` → `&amp;lt;p&amp;gt;…`。按「净化器都应幂等」写断言，或顺手"修成幂等"，都会改变网页端历史双重转义数据的展示。
2. `sanitize_rich_html` **25/25 完全幂等**（`convert_charrefs=True` + 统一重转义 + 自动补闭合），可以强断言。
3. `sanitize_rich_html` **保留纯文本节点里的** `javascript:` / `vbscript:` 字面量；危险模式清理只发生在标签、属性、`style` 上下文。断言「全文无危险词」不仅误报，还会诱导把正文合法文本一并清掉。

`to_plain_text` 25 组中 24 组幂等，唯一例外是 `double_escaped`（`&amp;lt;…` 一轮只还原一层实体）。这四条共同印证 8.5 的 B5 结论：三通道是**刻意分层**，不是冗余。

#### 8.6.2 落地的四项能力

| 能力 | 文件 | 做了什么 | 实测 |
| --- | --- | --- | --- |
| 全量校验网 | `tools/run_all_checks.py` | 自动发现 `tests/*.py` + `tools/*.py`；显式排除 29 个依赖缺失脚本并逐条注明原因；排除自身防递归；单脚本 90s 超时；`--list` / `--audit-excluded` 可审计；排除名单腐烂（文件消失）直接报错 | 175 发现 / 29 排除 / 1 自身 / **145 运行，0 失败**；CI 验证面 **14 → 145** |
| 架构指标 + 门槛 | `tools/architecture_report.py` + `tools/architecture_baseline.json` | 输出 17 项指标；只 gate 4 项坏味道（内联 script 行数/块数、循环内 DB 访问、最长函数）+ 1 项防回退（`ddl_helper_calls`）；规模类只报 delta 不 gate；`--init` 重建基线 | 5 项门槛全绿，基线 delta 全 0 |
| 热点基准 | `tools/bench_hotpaths.py` + `tools/bench_baseline.json` | 4 条**不依赖计时**的确定性性能契约（逐条 6 连接/6 提交 vs 批量 1 连接/1 提交、DDL 顺序、cursor 次数）+ 9 个热点 ns/op 报告；计时用同一进程内参考热点归一化比值，3.0x 容差只抓数量级退化 | 契约 4/4、比值门槛 6/6 全绿；`sqlite_batch_commit` 约 370 ns/op vs `sqlite_single_commit` 约 940 ns/op（≈2.5x），量化 B4 收益 |
| 净化属性测试 | `tests/test_sanitizer_properties.py` + `tests/sanitizer_golden.json` | 25×3 golden 逐字比对 + 幂等分层 + 输出结构不变量 + 截断上限 + 三通道分工边界 + Bark 实体乱码根治回归 | 21/0 |

**门槛为何这样设计**：CI 机器的绝对耗时不可跨环境比较，用「毫秒阈值」做门禁必然误报。因此硬断言全部落在**确定性事件计数**上；计时只做归一化比值报告，容差放到 3.0x，只抓 O(n) → O(n²) 这类数量级退化。规模类指标（行数）不设门槛，否则正常新增功能会让 CI 永久变红。

#### 8.6.3 架构基线

| 指标 | 基线 | 门槛 |
| --- | --- | --- |
| 运行时 Python `app/**/*.py` | 362 文件 / 72,676 行 | 只报告 |
| 模板 / 外链 JS | 44 文件 41,176 行 / 7 文件 4,855 行 | 只报告 |
| 内联 `<script>`（不带 src） | 31 个模板 / 44 块 / **23,734 行** | 不得增加 |
| 循环内 DB 访问（AST 节点去重） | **147** | 不得增加 |
| 最长函数 | **842 行** `app/plugins/user_backup/plugin.py::_setup_routes` | 不得增加 |
| `ensure_*` 定义 / 真实调用 | 42 / 163 | 只报告 |
| `ensure_registered_tables` 调用点 | 20 | 不得减少（防 B3-B 回退） |
| `execute_many` 调用点 | 0 | 只报告（梯队 1 入口） |
| provider 装配点 | 78 | 只报告 |

口径说明：与早前规模盘查（535 文件 / 109,074 行）数字不同，是因为此表只统计 `app/**` 运行时源码，不含 `tests/` / `tools/`；模板也只统计 `templates/**`。门槛只在**同一套口径**内比较，跨口径数字不具可比性。合法性增长需显式执行 `--init` 重建基线并在提交说明里写清原因，基线变更必须可 review。

#### 8.6.4 回归矩阵与明确不做的事

| 项 | 结果 |
| --- | --- |
| `tools/run_all_checks.py .`（全量自动发现） | 145/0 |
| `tools/verify_patches.py .`（含问题 21 组 10 条） | 248/0 |
| `tests/test_refactor_2_0_equivalence.py` / `tests/test_b_batch_equivalence.py` | 88/0 / 37/0 |
| `python -m compileall -q app/` / `node --check` / YAML 解析 / `git diff --check` | 全绿 |
| `git diff --name-only` 中 `app/**` 运行时源码 | **0 个文件**（零运行时代码改动） |

明确不做：不改版本号（仍 `2.0.2`）、不改 README（无功能与用法变化，主页约定见第 7 节）、不写 CHANGELOG 新版本、不打 tag、不发版、不动 `app/**`。CI verify 新增 4 个 step（3 项快速能力单独出报告，便于在 CI 日志里看指标趋势；最后 1 步做全量兜底）。

#### 8.6.5 下一梯队入口

`execute_many` 调用点仍为 0，而「同函数内 ≥2 次写」候选有 6 处——梯队 1 可直接复用 B4 的 oracle 证明等价。循环内 DB 访问 147 处、最长函数 842 行、内联 JS 23,734 行分别对应 P 批与 C3，须按 8.5 的前置条件推进。本批只把它们变成**可测量、可拦截**的指标，不提前动手。

---

## 9. 2.1：可证等价的运行时优化与发版

> 8.6 里「同函数内 ≥2 次写候选有 6 处」是早期粗估；9.2 用 AST 全量重数得到 **106 个函数**，并据此修正结论。

### 9.1 「完美」的可验证口径

用户要求「继续优化，直到完美发版 2.1」。把「完美」定义为可机器判定，避免无限重构：

1. **等价**：对同一输入集，新旧实现的可观察结果逐字节相同；golden **必须在改代码前采集**。
2. **覆盖**：主网 `run_all_checks` 自动发现全部纯标准库脚本且 0 失败；架构门槛、基准契约、静态断言全绿。
3. **边界**：被 100% 等价硬约束排除的项逐条写明原因，不编造完成。
4. **发版**：6 个版本点、CHANGELOG、README 版本锁定、CI 断言、tag 与镜像可回溯，CI verify/build/release 全绿。

不满足 ① 的改代码项一律不动；不满足 ③ 的项不算欠账。

### 9.2 探查结论（改代码前实测）

| 探查 | 实测 | 结论 |
| --- | --- | --- |
| 同函数内 ≥2 次写 | AST 统计 **106 个函数**（口径：函数体内 ≥2 次 `.execute` 调用） | **不做**。把逐条 `execute()` 改成 `execute_many()` 会把「每条独立提交」变成「整批单事务」，失败时 DB 残留完全不同，**事务边界与异常时机改变**，与逐字节等价互斥。`execute()` 源码一行未动，并把该状态写成 CI 断言（问题 22） |
| 循环内 DB 访问 | 147 处（架构门槛口径） | 不做：外提读会改变循环内状态可见性，需逐条并发证明 |
| 循环内字面量正则 | **16 处 / 7 文件 / 16 个不同 pattern**；同文件重复 ≥3 次的 pattern 0 个 | **可做**：`re.compile(p, f).op(...)` 与 `re.op(p, ..., flags=f)` 在标准库语义上等价，无副作用、无可观察状态变化 |
| 29 个排除脚本 | 隔离 venv 装齐 pytest / fastapi / PIL / anyio 后重跑：**11 通过 / 18 仍失败**；18 个里 16 个被本机沙箱 `sitecustomize` 拦截、2 个是 `app` 导入路径 | **不进 CI**。verify job 刻意保持纯标准库（快、稳、零依赖漂移）；11 个作为 C 批工程环境的前置证据列入 9.5，不为好看而让 CI 变脆 |

### 9.3 批次 1 落地：16 处循环内正则预编译外提

7 个文件 `+53 / -19`（净增 34 行常量定义），19 行替换覆盖 **21 个调用点**：16 处循环内 + 5 处同文件循环外同 pattern 重复（`notify_template` 有两行各含两个 `re.search`）：

| 文件 | 模块级常量 | 调用点 |
| --- | --- | --- |
| `app/domains/playback/dedupe.py` | `_QUALITY_MARKER_RE`（`re.IGNORECASE`） | 1 |
| `app/domains/playback/stats_helpers.py` | `_SEASON_S_RE`（`re.I`）/ `_SEASON_CN_NUM_RE` / `_SEASON_CN_HANZI_RE` | 3 |
| `app/domains/playback/user_details_router.py` | `_PLAY_DATETIME_RE` / `_PLAY_DATE_RE` | 2 |
| `app/plugins/hdhive/plugin.py` | `_CHECKIN_TIME_RE`（补模块级 `import re`） | 1 |
| `app/plugins/hdhivesign/plugin.py` | `_CHECKIN_TIME_RE` / `_SERVER_REF_RE` | 2 |
| `app/plugins/library_cover/plugin.py` | `_SAFE_FILENAME_RE` | 1 |
| `app/plugins/notify_template/plugin.py` | `_QUALITY_EMPTY_SEP_RE` / `_QUALITY_EMPTY_EOL_RE` / `_QUALITY_BLANK_PIPE_RE` / `_ENCODING_EMPTY_EOL_RE` / `_ENCODING_BLANK_EOL_RE` / `_ICON_ONLY_LINE_RE` | 6 |

等价性证明（`tests/test_loop_regex_equivalence.py` **53/0**）：

- golden `tests/loop_regex_golden.json` 在改代码前采集：16 组 spec × 4~8 个输入，记录 `sub/search/match/findall` 的 oracle 输出（`group0` / `groups` / `span` 全部序列化，避免只比布尔值漏判）。
- **行为层**：`re.compile(pattern, flags)` 重放与模块级 `re.op(..., flags=...)` 双向比对，输出逐字相同。
- **形态层**：AST 断言每个模块级常量的 pattern/flags 与 golden 一致、常量在函数体内被真实调用（`ast.Call` + `ast.Attribute`，不用裸子串）、7 个文件乃至 `app/**` 全库循环内字面量正则调用为 0；另断言「与 golden 同 pattern 的字面量重复调用已消失」与「21 个调用点全部走 16 个常量」。断言只覆盖本批 pattern，文件里 unrelated 的正则调用保留原样。

`stats_helpers`（3 处）、`user_details_router`（1 处）、`library_cover`（1 处）的循环外同 pattern 调用也复用同一常量，消除文件内重复。补充探查另发现循环内变量 pattern 调用 11 处（多为动态拼接或安全净化核心 `_DANGEROUS_PATTERNS`）、循环内常量容器赋值 67 处（多数是会被修改的业务 dict，外提会串改）、循环外单次字面量调用 51 处（`re` 模块有内部缓存，收益接近 0 而改动会扩到 30+ 文件）——三类均按「风险最小」排除，不做无依据改动。

`hdhive/plugin.py` 原本 `re` 只在函数内局部 import，为支持模块级常量补了模块级 `import re`；两处指向同一标准库模块对象，函数内局部 import 原样保留，行为不变。

### 9.4 治理能力增量

- `tools/architecture_report.py` 新增 `loop_literal_regex_calls` 指标并纳入不恶化门槛，基线重建为 **0**（16 → 0）：此后任何人在循环里写 `re.sub("字面量")` 都会被 CI 拦下。门槛由 5 项增至 **6 项**。
- `tools/verify_patches.py` 新增「问题 22」组 8 条断言：golden 覆盖、16 个常量（AST 计数）、循环内归零、CI 接线、门槛基线、以及 **`execute()` 事务边界未被改成 `execute_many`**。
- CI verify 新增 1 个 step（22 → 23 步）；`run_all_checks.py` 自动发现把新测试纳入主网，**无需手动接线**。
- 架构基线因新增指标 + 运行时净增 34 行而显式 `--init` 重建；规模类只报 delta，不 gate。

### 9.5 明确排除（不构成欠账）

| 项 | 排除理由 | 后续前置 |
| --- | --- | --- |
| 106 个同函数多写 → `execute_many` | 事务边界 / 失败残留不等价 | 需授权「失败整批回滚」作为行为变更，或逐个证明上层已有事务包裹 |
| 147 处循环内 DB 访问 | 外提读会改变状态可见性 | 并发写压测 + DB 终态快照 |
| 842 / 687 / 450 行巨型函数拆分 | 需 DOM / API / DB / provider 四层快照 | pytest + FastAPI 集成环境（8.5） |
| 内联 JS 23,734 行 / C5 `unsafe-eval` | 移除 `unsafe-eval` 会让 Alpine 表达式失效 | C 批工程环境 |
| 29 个排除脚本进 CI | 18 个仍因沙箱 / 导入路径失败；CI 保持纯标准库 | 在 CI 装齐全依赖并逐个证明稳定，另起一轮 |
| B2 / B5 / 迁移型 `ensure_*` | 见 8.5，统一与输出逐字不变互斥 | 见 8.5 |

### 9.6 发版 2.1 与回归

**三提交分工**（沿用 2.0.2：优化与发版分离，**发版 commit 零运行时代码改动**）：

- `958401e`：批次 1 优化，14 文件 `+1069 / -23`，其中运行时 7 文件 `+53 / -19`，其余为 golden / 测试 / 治理脚本 / 基线 / 文档。
- `ed87397`：发版 commit，只改 6 个版本点、CHANGELOG、README 版本锁定、版本断言与本文件 9.6；`app/**` 中**只有** `app/shared/version.py` 的版本常量变化，业务代码一行不动。该 commit 推送后 CI verify 因下述历史脚本失败，未产生 Release / 镜像。
- CI 收口 commit：只改 `tests/test_tgtdrive_monitor.py`（绝对路径 → 按 `__file__` 仓库相对定位）、`tools/run_all_checks.py`（新增可移植性硬门禁）与两份文档；`app/**` 一行不动，业务行为零变化。`v2.1` tag 重建指向此 commit。

**6 个版本点** `2.0.2` → `2.1`：`app/shared/version.py`、`Dockerfile`（`2.1-hardened`）、`docker-compose.yml`（`IMAGE_TAG:-2.1`）、`templates/request.html`（`request_app.js?v=2.1`）、`CHANGELOG.md`（新增 `## 2.1`）、`README.md`（默认拉取与 compose 片段）。`tools/verify_patches.py` 与 `tests/test_refactor_2_0_equivalence.py` 的版本断言同步。

**tag 策略**：创建 annotated tag `v2.1` 并推送；`v2.0` / `v2.0.1` / `v2.0.2` 一律不重推，`IMAGE_TAG=1.9.10` 回退路径保留。主页按既有约定只同步版本锁定，功能清单与用法不展开。

**首次 tag 的 CI 失败与重建**：`ed87397` 推送后 verify job 唯一失败项为 `tests/test_tgtdrive_monitor.py`（145/1）。根因是该老脚本第 40 行硬编码 `/tmp/ep_repo/tgtdrive-monitor/plugin.py`：macOS 的 `/tmp` 是 `/private/tmp` 符号链接，本机通过；CI checkout 在 `/home/runner/work/...`，`importlib` 的 `get_code` 找不到文件直接抛异常。该脚本此前从未进 CI（verify 只显式接 14 个），梯队 0 的全量网首次把它纳入才暴露。因该 tag 尚无 Release、无 GHCR 镜像、无人使用，经确认删除并重建 `v2.1` 指向全绿 commit；**历史 tag 与 `main` 提交历史不动**，并新增门禁防复发。

**回归矩阵**：

| 项 | 结果 |
| --- | --- |
| `tools/run_all_checks.py .`（自动发现 146 项） | 146/0 |
| Python 3.12（CI 同版本）干净 worktree + 主仓复跑 | 146/0；3.13 亦 146/0 |
| 可移植性门禁探针（注入 `/tmp/ep_repo` 临时脚本） | rc=1 拦下，清理后恢复 146/0 |
| `tools/verify_patches.py .`（问题 21 / 22 组） | 256/0 |
| `tests/test_loop_regex_equivalence.py` | 53/0 |
| `tests/test_sanitizer_properties.py` / `test_refactor_2_0_equivalence.py` / `test_b_batch_equivalence.py` | 21/0 / 88/0 / 37/0 |
| `tools/architecture_report.py` 6 项门槛 | delta 全 0，全绿 |
| `tools/bench_hotpaths.py` | 4 条确定性契约 + 6 项比值全绿 |
| `compileall` / `node --check`（7 个 JS）/ compose+workflow YAML / 凭证扫描 / `git diff --check` | 全绿 |

**9.1 完成口径对照**：① 等价 —— golden 前置采集 + 53/0 双向比对 + 88/0 / 37/0 既有等价性回归，运行时改动逐处可证明；② 覆盖 —— 主网 146/0、6 项门槛、256/0 静态断言；③ 边界 —— 9.5 逐条写明排除理由，并固化「事务边界不可改」等 CI 断言；④ 发版 —— 6 个版本点、CHANGELOG、README 锁定、tag 与 CI 三 job（verify / build / release）全绿可回溯，Release 由 CI 自动创建。四条全部满足，2.1 收口。
