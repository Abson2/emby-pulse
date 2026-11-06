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

**兼容性**：所有旧版本标签（`v1.9.10` 等）与 `IMAGE_TAG` 回退路径保持不变；对外 API、模板变量、配置键、DB schema 均不改。

---

## 6. 后续批次排队

本轮交付 = 「完整分析 + 批次 A 代码 + B0 乱码修复 + 安全加固 + 等价性测试 + 2.0 版本号 + 全量回归」。
下一轮建议按 `B0 收口（push_user_notify 层统一纯文本化）→ B1（library_common 抽离）→ B3（ensure_once）→ B2（前端公共 JS）` 推进，每轮单独提交。
C 批建议等 CI 具备 pytest + FastAPI 集成环境后再启动。
