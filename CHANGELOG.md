# 更新日志

## 2.1

可证等价的性能优化与治理能力完善版（v2.0.2 之后）。**零功能变化**：对外接口、原有输入输出行为、配置键、数据库 schema 均未改动，功能与 `2.0.2` / `1.9.10` 完全等价。本版把「完美」定义为可机器判定：等价（golden 前置采集 + 逐字节比对）、覆盖（全量校验网 0 失败 + 6 项架构门槛）、边界（排除项逐条写明理由）、发版（版本点 / tag / CI 可回溯）。完整决策与客观边界见 [docs/REFACTOR_2.0.md](docs/REFACTOR_2.0.md) 第 9 节。

### 1. 运行时优化：21 处循环内 / 重复正则预编译外提

- 改代码前采集 `tests/loop_regex_golden.json`：16 组 pattern/flags/输入/oracle，覆盖 `sub` / `search` / `match` / `findall`，输出含 `group0` / `groups` / `span` 全量序列化，不只是布尔匹配。
- 7 个文件 `+53 / -19`：16 处循环内字面量正则 + 5 处同文件循环外同 pattern 重复，共 **21 个调用点收敛到 16 个模块级 `re.compile` 常量**。涉及播放去重季号解析、用户明细时间解析、影巢签到时间/资源解析、封面文件名净化、通知模板逐行过滤。
- `hdhive/plugin.py` 补模块级 `import re`；函数内原有的局部 `import re` 原样保留，两者指向同一标准库模块对象，行为不变。
- **为什么等价**：`re.compile(p, f).op(...)` 与 `re.op(p, ..., flags=f)` 在标准库语义上完全一致，无副作用、无可观察状态变化；`tests/test_loop_regex_equivalence.py` 53/0 做 golden 行为重放 + 模块级调用双向比对 + AST 形态断言。

### 2. 治理能力完善

- `tools/architecture_report.py` 新增 `loop_literal_regex_calls` 指标并纳入不恶化门槛，基线 **16 → 0**；此后在循环里写 `re.sub("字面量")` 会被 CI 拦下。门槛由 5 项增至 **6 项**。
- `tools/verify_patches.py` 新增「问题 22」组 8 条断言（**256/0**）：golden 覆盖、16 个常量（AST 计数）、循环内归零、CI 接线、门槛基线、以及 **`execute()` 事务边界未被改成 `execute_many`**。
- CI verify 新增 1 个 step（22 → 23 步）；新测试由 `tools/run_all_checks.py` 自动发现纳入主网（**146/0**），无需手动接线。
- `docs/REFACTOR_2.0.md` 新增第 9 节：完成口径、探查数据、等价性证明、治理增量、明确排除项、发版与回归矩阵。

### 3. 探查结论与明确排除（不构成欠账）

- AST 全量重数「同函数内 ≥2 次写」为 **106 个函数**（此前粗估 6 个是低估）。把逐条 `execute()` 改成 `execute_many()` 会把「每条独立提交」变成「整批单事务」，失败时 DB 残留完全不同，**事务边界与异常时机改变**，与逐字节等价互斥；`execute()` 源码一行未动，并把该状态写成 CI 断言。
- 隔离 venv 装齐 `pytest` / `fastapi` / `PIL` / `anyio` 后重跑 29 个排除脚本：**11 通过 / 18 仍失败**（16 个被本机沙箱 `sitecustomize` 拦截、2 个是 `app` 导入路径）。CI verify 刻意保持纯标准库，不为了扩面引入依赖漂移；11 个作为后续工程环境建设的前置证据。
- 循环内变量 pattern 调用 11 处（多为动态拼接或安全净化核心）、循环内常量容器赋值 67 处（多数是会被修改的业务 dict，外提会串改）、循环外单次字面量正则 51 处（`re` 模块有内部缓存，收益接近 0 而改动会扩到 30+ 文件）——均按「风险最小」排除。
- B2 / B5 / 迁移型 `ensure_*` / C 批大重构的前置条件见第 8.5 节，结论不变。

### 4. 回归矩阵

| 项 | 结果 |
| --- | --- |
| `tools/run_all_checks.py .`（自动发现） | 146/0（29 排除 / 1 自身） |
| `tools/verify_patches.py .` | 256/0（问题 21 / 22 组） |
| `tests/test_loop_regex_equivalence.py` | 53/0 |
| `tests/test_sanitizer_properties.py` / `test_refactor_2_0_equivalence.py` / `test_b_batch_equivalence.py` | 21/0 / 88/0 / 37/0 |
| `tools/architecture_report.py` / `tools/bench_hotpaths.py` | 6 项门槛、4 条确定性契约 + 6 项比值全绿 |
| `compileall` / `node --check`（7 个 JS）/ YAML / 凭证扫描 / `git diff --check` | 全绿 |

### 5. 版本与发版

- 版本号 `2.0.2` → `2.1`：`app/shared/version.py`、`Dockerfile`（`2.1-hardened`）、`docker-compose.yml`（默认 `IMAGE_TAG:-2.1`）、`templates/request.html`（`request_app.js?v=2.1`）、README、CHANGELOG；`verify_patches.py` 与等价性测试的版本断言同步更新。
- **`v2.0` / `v2.0.1` / `v2.0.2` tag 与已发布镜像不重推**；`IMAGE_TAG=1.9.10` 回退路径保持不变。
- **GitHub 主页按既有约定保持简洁**：本次零功能变化，功能清单与当前使用说明不变，仅同步版本锁定；各版本详细更新说明以本文件与 GitHub Releases 为准，主页不展开。

## 2.0.2

主线架构优化收口版（v2.0.1 之后）。**零功能变化**：全部改动为内部等价重构、测试补强与文档同步，对外接口、原有输入输出行为、配置键、数据库 schema 均未改动，功能与 `2.0.1` / `1.9.10` 完全等价。按「风险最小、稳定性、安全性、100% 功能保留」推进，每批独立可 revert。完整决策记录与客观边界见 [docs/REFACTOR_2.0.md](docs/REFACTOR_2.0.md) 第 8 节。

### 1. 公共逻辑抽离（B1）

- 新增 `app/bot/notification_bot/_library_common.py`：两个双胞胎入库通知 service 的 provider 装配样板（48 行 → 15 行）与 `_format_overview` / `_format_type` / `_get_base_url` / `_get_platform` 四个同构纯函数收敛为薄包装，两文件 291+329 → 249+293 行，净减 78 行重复。
- 公共模块**不持有任何 provider 状态**，`apply_dependency_providers(globals(), ...)` 只写调用方命名空间，两个 service 各自注入的桩仍互不串扰。
- `tools/check_notification_bot_subscription.py` 的 AST 抽取命名空间补入真实公共模块（该脚本忽略 import 语句），既有订阅链路 29/0 恢复全绿。

### 2. DDL 门控与重复消除（B3 / B3-B）

- 新增 `app/infra/db/ensure_once.py` 通用一次性门控（按 key 缓存 / 异常不写缓存 / reset / 线程锁）；`message_dao.ensure_announcement_tables()` 迁入，原有模块级状态名与 `reset_announcement_tables_cache()` 全部保留。
- 全库 40 个顶层 `ensure_*` 完整分类，严格 AST 匹配出 20 个逐字同构的纯建表样板（18 个 DAO 文件），新增无状态 `app/infra/db/ddl_bootstrap.py`，调用点收敛为一行，`+38/-86` 净减 48 行重复。连接次数 / cursor 创建 / DDL 顺序 / commit 次数 / 异常传播逐事件等价。
- **不引入缓存**：带 PRAGMA / ALTER / 业务 SQL 的迁移型 `ensure_*` 一律不接入。

### 3. 性能与安全（B4 / S5 / S6）

- `SystemStore.execute_many()` 纯加法：多条写语句共用 1 连接 1 事务，失败整批回滚；`execute()` 逐条提交语义一行未动。
- `sanitize_rich_html()` 对 `style` 清洗 `url()` / `expression()` / `@import` / `behavior:` / `-moz-binding`，合法声明逐字不变（只收紧非法输入）。
- 表名 f-string 拼接由集中注册表管理，静态断言证明非用户输入。

### 4. 等价性验证与回归

- 新增 `tests/test_b1_library_common_equivalence.py`（53/0）与 `tests/test_b_batch_equivalence.py`（37/0，含 B3-B 第 5 节旧骨架 oracle 逐事件比对），两者接入 CI。
- `tests/test_refactor_2_0_equivalence.py` 88/0；`tools/verify_patches.py` 238/0（新增问题 19 / 20 组）；`tools/check_security_contracts.py` 44/0。
- **静态断言纪律**：判断调用存在 / 不存在一律用 `ast.Call` + `ast.Name`，不用裸子串（避免 helper 定义行自匹配，B3-B 的 21→20 与第二批 87/1 同源）。
- 全量 171 个 `tests/*.py + tools/*.py` 脚本与 HEAD 干净基线对比，失败集合完全一致，未引入任何新失败；CI 官方 14 项、`compileall`、`node --check`、YAML 解析、凭证扫描全绿。

### 5. 版本与文档同步

- 版本号 `2.0.1` → `2.0.2`：`app/shared/version.py`、`Dockerfile`（`2.0.2-hardened`）、`docker-compose.yml`（默认 `IMAGE_TAG:-2.0.2`）、`templates/request.html`（`request_app.js?v=2.0.2`，每次发版必改）、README、CHANGELOG；`verify_patches.py` 与等价性测试的版本断言同步更新。
- **`v2.0` / `v2.0.1` tag 与已发布镜像不重推**（避免已部署实例、Release 与镜像摘要漂移）；`IMAGE_TAG=1.9.10` 回退路径保持不变。
- **GitHub 主页按既有约定保持简洁**：本次零功能变化，功能清单与当前使用说明不变，仅同步版本锁定；各版本详细更新说明以本文件与 GitHub Releases 为准，主页不展开。

## 2.0.1

零风险补丁版（v2.0 之后）。按「风险最小、稳定性、安全性、100% 功能保留」筛选，只做确凿同类 bug 修复、安全契约固化与测试补强，**不改对外接口、不删任何特性、不做 DI 容器 / 前端公共 JS / DDL 通用化等运行时行为大改**。完整决策记录见 [docs/REFACTOR_2.0.md](docs/REFACTOR_2.0.md) 第 7 节。

### 1. 同类乱码收尾（管理员消息用户级通知）

- 现象与公告同源：`send_message()` 给用户的 Bark / Webhook 推送正文走 `sanitize_html()` 做 `html.escape`，纯文本通道不解码实体，`&` / `<` / `>` 会以 `&amp;` / `&lt;` / `&gt;` 字面量送达。
- 修复：该处唯一的用户级通知调用改用 `to_plain_text(data.content, max_length=5000)`；**网页端 `insert_admin_message()` 存储与展示链路仍用 `sanitize_html()`，一行未动**；对外参数、`flag_key`、异常吞噬语义全部不变。
- 刻意不动其它用户级通知入口：新片入库 / 剧集更新 / 心愿状态变动的正文是模板渲染的真实 HTML 标签，Bark 出口 `_strip_html()` 已能正常剥离，改动反而会引入行为差异。

### 2. 安全契约固化为 CI 可执行断言

- 新增 `tools/check_security_contracts.py`（纯标准库、只读源码、42/0）：把 CSRF / 反向代理头、AuthGate 默认拒绝与中间件顺序、10307/10308 Cookie 隔离、CSP nonce、SSRF 代理运行期校验、路径穿越、敏感字段 env 中央注册、日志脱敏、富文本净化 / 纯文本通道、CI 防护链固化为断言；第 9 节直接加载 `security_utils.py` 做行为级净化验证。
- CI `verify` job 接入该脚本与 `tests/test_sanitize_rich_html.py`。

### 3. 回归与边界

- `tests/test_refactor_2_0_equivalence.py` 扩展第 8 节（88/0）：AST 精确抽取 `push_user_notify` 调用验证正文替换与参数不变、网页端存储未动、三个未改动入口仍在、用户级通知服务签名不变、安全契约脚本只用标准库（AST import 白名单，避免裸子串自匹配误伤）、CI 接线、2.0 版本号稳定。
- `tools/verify_patches.py` 新增「问题 17」（第二批边界）与「问题 18」（主页文档收敛）两组断言，224/0。
- 全套既有回归、`compileall`、`node --check`、YAML 解析、凭证扫描保持全绿。

### 4. 版本与文档同步

- 版本号 `2.0` → `2.0.1`：`app/shared/version.py`、`Dockerfile`（`2.0.1-hardened`）、`docker-compose.yml`（默认 `IMAGE_TAG:-2.0.1`）、`templates/request.html`（`request_app.js?v=2.0.1`，每次发版必改）、README、CHANGELOG。`verify_patches.py` 与等价性测试的版本断言同步更新。
- **`v2.0` tag 与已发布镜像不重推**（避免已部署实例、Release 与镜像摘要漂移）；`IMAGE_TAG=1.9.10` 回退路径保持不变。
- **GitHub 主页文档同步收敛**：README 保持原 4 段式结构，第二节由按版本罗列的修复史改为按能力归类的功能清单，第三节去掉各版本根因 / 症状说明只留当前用法；每项能力的详细更新说明仍以 CHANGELOG 对应版本与 GitHub Releases 为准，主页不展开。`verify_patches.py` 新增「问题 18」断言组固化该约定。

## 2.0

架构优化与安全性审查版。**硬约束：100% 保留全部现有功能、原有输入输出行为不变、不改动对外接口、不删除任何特性**。本版为「低风险清理」首批，完整梳理与后续批次方案见 [docs/REFACTOR_2.0.md](docs/REFACTOR_2.0.md)。

### 1. 架构梳理与重构方案（文档交付）

- 梳理出规模：Python 106,967 行 / 模板 41,137 行 / JS 5,691 行 / 75 张表 / 43 个 notification_bot service / 15 个插件。
- 明确 8 类耦合点（`bot_service.py` 上帝装配器 40 次注入、116 处 `set_dependency_providers` 样板、全局配置单例、跨域反向依赖、DDL 与请求路径混跑、展示层无公共模块、插件同构、巨型函数）。
- 明确 8 类冗余（10 处 `escapeHtml`、3 处不一致的 `parseMarkdown`、2 个同名 `ensure_request_admin_messages_table`、双胞胎 library service 等）。
- 明确 7 类性能瓶颈（每条 SQL 新建连接、每请求 DDL、日志裁剪全表比对等）。
- 给出【低风险清理 / 中风险抽离 / 大架构重构】三批方案与优化顺序；本版只落地第一批，后续批次按风险排队，不做无法证明等价的大改。

### 2. 性能优化（逻辑等价）

- `app/plugins/plugin_dao.py::add_plugin_log()`：INSERT + 行数裁剪 + 时间清理三条语句合并为**单连接单事务**（3 次 `sqlite3.connect`/commit → 1 次），行数裁剪 `NOT IN (子查询)` 改为**主键范围删除** `id < MIN(最近 N 条)`，不再对全表做比对。函数返回、三语义（单条 2000 字截断 / 500 行上限 / 30 天留存）完全不变。
- `app/infra/db/schema_registry.py`：`plugin_logs` 增补 `idx_plugin_logs_created_at`（`CREATE INDEX IF NOT EXISTS` 纯加法），时间清理不再逐插件全表扫描。
- `app/domains/notifications/message_dao.py::ensure_announcement_tables()`：DDL 幂等，改为**进程内一次性缓存**（键为 `system_store.db_path`，换库自动失效，带线程锁），并提供 `reset_announcement_tables_cache()` 供测试/迁移使用；DDL 异常时不写缓存，下次自动重试。公告 6 个端点不再每请求重复建表/建索引。
- `app/domains/notifications/announcements_router.py`：两个 GET 接口的历史数据解码循环抽为 `_decode_rows_content()` 公共 helper（纯内部去重，返回结构与字段不变）。

### 3. 安全性审查与加固

- 全库审查认证/鉴权、CSRF、会话隔离、CSP nonce、SSRF 代理校验、路径穿越、SQL 注入、XSS、命令注入、敏感信息日志。确认既有防护（CSRF / AuthGate / Cookie 隔离 / CSP / SSRF / 路径穿越 / 敏感字段 env 中央注册）完好且未削弱；SQL f-string 拼接处均为内部常量或注册表白名单，用户输入全部走 `?` 绑定，非注入漏洞。
- **用户端 `x-html` 富文本统一净化**：`templates/request.html` 引入 DOMPurify（带 CSP nonce），新增全局 `sanitizeRichText()`；欢迎消息与公告详情的 `x-html` 均经其净化。**DOMPurify 未加载或异常时降级返回原值**，合法内容渲染与旧版一致、功能不破。
- **日志脱敏**：`app/core/security_check.py` 不再输出新生成 Token 的前 8 / 后 8 字符片段；`app/core/config.py` 启动日志不再打印 `emby_host` 内网地址明文（改为「已设置/未设置」）。
- `sanitize_html` 与新增 `to_plain_text` 共用 `_DANGEROUS_PATTERNS`，消除危险模式清单重复。

### 4. Bug 修复：用户级公告 Bark 推送 `&lt;p&gt;` 实体乱码

- 现象：网页端公告显示正常，但用户收到的 Bark 推送为 `&lt;p&gt;测试推送&lt;/p&gt;` 字面量。
- 根因：`create_announcement()` 的用户级通知正文走 `sanitize_html()` 做了 `html.escape`，把 Quill 富文本 `<p>测试推送</p>` 转成 HTML 实体；Bark 是纯文本通道，**既不渲染标签也不解码实体**，而 `bark_push._strip_html()` 只能剥离真实标签，无法处理实体。网页端是另一条链路（富文本 + `x-html` 渲染），故显示正常。
- 修复：`app/core/security_utils.py` 新增 `to_plain_text()`（截断 → 实体反转义 → 剥离标签 → 危险模式清理），公告用户级通知的正文/标题改用它；网页端存储仍用 `sanitize_rich_html` 保留富文本，**显示链路完全未动**。纯文本输入输出与旧版逐字一致；含 `&`/`<`/`>` 的纯文本不再被错误实体化（同属乱码修复）。

### 5. 回归与验证

- 新增 `tests/test_refactor_2_0_equivalence.py`（纯标准库，70/0 全绿），用**内存 SQLite 双库并行**逐行比对新旧日志实现的最终表内容（边界 N=1/2/7/500、多插件隔离、留存清理、单条截断），并用 AST 抽取执行验证 helper / 日志脱敏 / 索引常量，静态契约锁定对外接口与前端净化接线。已接入 CI `verify` job。
- `tools/verify_patches.py` 新增「问题 16」断言组（2.0 优化 / 安全 / 乱码修复 / 接口不变），全绿；其余既有回归全绿。
- 版本号同步：`2.0`、`APP_VERSION=2.0-hardened`、compose 默认 `2.0`（保留 `IMAGE_TAG=1.9.10` 回退）、`request_app.js?v=2.0`。

### 升级后验证清单

1. 公告网页端（管理端列表 / 用户端详情弹窗）富文本显示与 1.9.10 一致；
2. 发布一条公告，用户级 Bark 推送正文为「测试推送」，**不再出现 `&lt;p&gt;` / `&amp;` 等实体字面量**；
3. 插件页「日志」仍能正常写入与查看，上限（500 行 / 30 天 / 2000 字）不变；
4. 容器日志不再出现 Token 片段或 `emby_host` 明文；
5. 所有既有功能与 API 行为不变；如需回退 `IMAGE_TAG=1.9.10`。

## 1.9.10

围绕「推送体验」与「公告显示」四项落地修复，均为发布后反馈驱动，不改动入库推送主链路语义。

### 1. Bark 新片入库推送乱码根治（纯文本通道剥离 HTML）

用户反馈新片入库 Bark 推送出现 `<b>` 字面量与啰嗦。经核查「自定义通知模板 v1.1」插件已正常生效，乱码根因是 **Bark 是纯文本通道、不渲染 HTML**，而新片入库 Bark 路径此前漏调 HTML 剥离。

- `app/domains/notifications/bark_push.py::send()` 在统一出口层对 `title` / `body` 调用 `_strip_html()` 后再截断长度，根治新片入库 / 用户级广播 / 规则通知等所有经 Bark 出口的 `<b>` 字面量问题。
- **不改动默认通知模板**：模板简洁化由用户自行在插件中心「自定义通知模板 v1.1」编写自定义模板完成（用户明确选择此路径），本版本只负责把 HTML 标签在 Bark 纯文本出口剥离。
- 回归：`tests/test_bark_push.py` 新增 5 条 HTML 剥离断言（新片入库 / send 统一出口 / 用户级 / 规则通知二次剥离），全绿。

### 2. 管理端 Bark Device Key 支持环境变量 BARK_DEVICE_KEY（与 WEBHOOK_TOKEN 同级安全处理）

- `app/core/config.py` 的 `SENSITIVE_ENV_FIELDS` 新增 `"bark_device_key": "BARK_DEVICE_KEY"`，复用既有「env 强制覆盖 / 不落盘 / 来源判定 / 保存拒绝 / 读取回真实值」整套机制。
- `app/infra/config/notification_settings.py::get_bark_config()` 新增 `device_key_source` / `device_key_readonly`：env 来源返回「****（由环境变量设置）」占位符且只读，config 来源返回掩码值；本模块不自行读取环境变量，统一走 `cfg.get_env_source` 判定。
- 前端 `templates/bot.html` / `templates/notify_admin.html` 在 env 来源下清空输入框、设置只读与占位符。
- `.env.example` 新增 `BARK_DEVICE_KEY` 说明（仅管理端系统级 Bark，可选；用户端各自粘贴 `bark_url` 不受影响）。
- 范围：**仅管理端系统级 Bark**，不改动用户端 `user_notify_service.py`。

### 3. 影视通知写入插件日志 + 上限管控（避免冗长）

用户要求「自定义通知模版插件推送的影视通知需要在插件日志中体现，且日志做大小、时间上限管控」。

- `app/plugins/plugin_dao.py::add_plugin_log()` 新增管控：单条 `message` 截断至 2000 字符；表超 500 行时删除最旧记录；超 30 天留存期的记录定期清理。
- `app/plugins/notify_template/plugin.py` 新增 `log_media_notification(type_cn, name, style, detail="")`，影视通知渲染后调用，显式 `notify=False`（不触发全局通知刷屏），仅写入插件日志表（插件页「日志」可见，用于核对插件是否生效）。
- `app/bot/notification_bot/notification_bot_library_new_item_service.py::handle_library_new_item()` 每部新片渲染后调用一次，记录类型 / 名称 / 风格（自定义模板 / 默认模板）。

### 4. 公告富文本乱码修复（`&lt;p&gt;欢迎大家&lt;/p&gt;` 字面显示）

用户通过管理端公告管理发布公告后，内容显示为 `&lt;p&gt;欢迎大家&lt;/p&gt;` 字面量。根因：**`sanitize_rich_html` 依赖第三方库 `bleach`，但 `bleach` 从未列入依赖**，ImportError 退化为 `html.escape()`，把 `<p>...</p>` 整体转义成 `&lt;p&gt;...&lt;/p&gt;` 存入数据库，前端渲染时露出实体。

- `app/core/security_utils.py::sanitize_rich_html()` 重写为**零依赖自实现**的标签白名单净化器（基于标准库 `html.parser`），保留 `p/strong/em/u/s/a/span/ul/li/br/h1-6/img/...` 等安全标签与 `href/style` 等安全属性，剥离 `script` / `onerror` / `javascript:` 等危险内容，对所有文本节点做 HTML 转义——与历史 bleach 配置语义对齐，但不再二次转义。
- 新增 `decode_announcement_content()`：仅对「不含真实标签字符、却含 `&lt;`/`&gt;` 实体」的历史二次转义数据解码一次还原；真实富文本原样返回，避免误伤。
- `app/domains/notifications/announcements_router.py` 的两个 GET 接口（管理端列表 / 用户端列表）在读取时对 `content` 做兼容解码，历史公告无需改库即正常显示。
- 回归：`tests/test_sanitize_rich_html.py` 新增（保留标签 / 剥离危险 / 实体兼容三类断言），全绿。

### 发布与验证

- 应用版本升级至 `1.9.10`；Dockerfile `APP_VERSION=1.9.10-hardened`；compose 默认锁定 `1.9.10`（保留 `1.9.9` 回退）；`request_app.js?v=1.9.10` 同步递增。
- 升级后验证清单：① 新片入库 Bark 推送不再出现 `<b>` 字面量；② 配置 `BARK_DEVICE_KEY` 环境变量后，机器人助手 / 通知设置页 Device Key 自动只读；③ 发布/编辑一条富文本公告，管理端列表与用户端详情弹窗正确渲染段落与加粗（不再显示 `&lt;p&gt;` 实体）；④ 发布新片后，自定义通知模板插件「日志」中出现一条「影视通知渲染」记录。

## 1.9.9

修复 1.9.8 上线后生产日志里出现的两条 `[Webhook] 未识别 Event=` 告警（`library.deleted` 与 `system.notificationtest`），并顺带抑制一批 Emby 高频系统/会话事件的噪声。属日志降噪与误判防护，不涉及入库推送主链路改动。

### 背景：两个告警都不是入库事件

1.9.8 上线后容器日志出现：

```
[2026-11-06 17:58:26] WARNING:  [Webhook] 未识别 Event=library.deleted (归一化=library.deleted)；若为入库事件请补充映射表
[2026-11-06 18:03:14] WARNING:  [Webhook] 未识别 Event=system.notificationtest (归一化=system.notificationtest)；若为入库事件请补充映射表
```

两条事件都是 Emby 发出、但**不是入库事件**：

- `library.deleted`（Emby `LibraryDeleted`）：整个媒体库被移除，不是单项媒体被删；下游「媒体被删除」通知（`notify.item.deleted`）面向的是带 `Type`/`Name` 的单个媒体条目，把它当单项删除处理会推出「未知资源被删除」的误导消息。
- `system.notificationtest`：Emby 后台「发送测试通知」按钮触发的纯测试事件，无任何业务含义。

按 1.9.8 告警自身的提示「若为入库事件请补充映射表」——两者都不是入库事件，因此正确做法是**静默忽略**，而不是塞进 `EMBY_TO_INTERNAL` 映射表。

### 误判风险（关键）

`library.deleted` 归一化后是 `library.deleted`，其中包含子串 `delete`；而下游 `notification_bot_webhook_event_service.handle_webhook_event()` 用 `"delete" in event or "remove" in event` 来判定是否发「媒体被删除」通知。若只是让它落进事件总线、只压制告警，它仍会被误匹配成删除通知。因此 1.9.9 在 `webhook.py` 入口对已知非业务事件做**早返回**：既不打印告警、也**不向事件总线发布**，从源头切断误判。

### 改造：`webhook_events.py` 新增 `IGNORE_EVENT_KEYS` 与 `is_ignorable_event()`

- `IGNORE_EVENT_KEYS`：已归一化（去分隔符小写）的事件名集合，收录 `librarydeleted` / `systemnotificationtest` / `systemwebhooktest` / `sessionstarted` / `sessionstopped` / `userdatasaved`。只放「确实非入库、无业务动作」的事件；若日后出现新的入库类事件，应进 `EMBY_TO_INTERNAL` 映射表，而不是这里。
- `is_ignorable_event(raw)`：用 `event_key()` 归一化后查集合，大小写/分隔符无关。
- `webhook.py::emby_webhook` 在计算 `event` 之后、重要事件日志与告警之前插入早返回：`is_ignorable_event(raw_event)` 命中即记一条 debug 并返回 `{"status": "success", "message": "ignored"}`，不告警、不发布、不触发播放 IP 保存等后续分支（这些事件也不承载对应动作）。

`EMBY_TO_INTERNAL` 中真实媒体删除事件（`itemremoved` / `itemdeleted` / `libraryitemremoved` / `libraryitemdeleted` → `remove`）保持不变，仍会正常走「媒体被删除」通知链路（该通知默认关闭，由 `notify_item_deleted` 开关控制）。

### 回归

- `tests/test_webhook_events.py` 新增第 7 节「已知非业务事件抑制（1.9.9）」：验证 `library.deleted` / `system.notificationtest` / `SessionStarted` 被判定为可忽略、`ItemAdded` / `LibraryChanged` / `ItemRemoved` 不被误判；复刻 `webhook.py` 告警门控确认两者既不告警也不发布；并反向确认真正未知的 `foo.bar` 仍会按名称限流告警。断言由 47 项增至 58 项，全绿。
- `tools/verify_patches.py` 新增「问题 15」断言组（忽略集合 / `is_ignorable_event` / `webhook.py` 早返回门控 / 告警前插入 / CI 接入 / CHANGELOG），并把全部版本断言升至 1.9.9。
- 全量回归：`compileall`、`verify_patches`、`check_bark_channel_contract`、`test_bark_push`、`test_webhook_events`、`test_user_notify`、`test_tg_whitelist_sync`、`check_notification_bot_subscription`、`check-no-secrets`、模板内联 JS `node --check`、YAML 解析。

### 发布与验证

- 应用版本升级至 `1.9.9`；compose 默认锁定 `1.9.9`，`1.9.8` 仍保留，可用 `IMAGE_TAG=1.9.8` 回退；`request_app.js?v=1.9.9` 同步递增。
- 升级后验证清单：① 触发一次 Emby 后台「发送测试通知」或让 Emby 移除一个媒体库，容器日志**不应再出现** `[Webhook] 未识别 Event=library.deleted` / `system.notificationtest`；② 这两条事件也不应产生任何「媒体被删除」推送（无论 `notify_item_deleted` 是否开启）；③ 入库推送主链路不受影响——真实新片入库仍应出现 `🔔 触发事件: item.added` 并正常推 Bark / 用户端通知；④ 若仍出现**其它**未识别事件告警，把事件名反馈回来评估是否补进映射表或忽略集合。

## 1.9.8

修复 1.9.7 升级后「用户端能收管理员通知、但收不到新片入库；管理端 Bark 也收不到新片入库」的问题。根因在上游 Webhook 事件入口，与 Bark 配置、渠道开关、用户级推送链路均无关；依据用户侧 NAS 排查结论与资料库 `embypulse_bark_fix.md` 第四节实施源码改造（方案 A，治本）。

### 根因：Emby 事件名与载荷双重不匹配

- 现象自洽性：用户端「管理员消息 / 全站公告 / 心愿变动」正常，唯独「新片入库」静默；系统级 Bark 也只缺入库。系统级 Bark 与用户级「新片入库」广播共用 `handle_library_new_item()`，上游断了两者同时不触发，而管理员消息走另一条链路所以正常。
- Emby Webhooks 插件发出的是 `ItemAdded` / `LibraryChanged`（驼峰），且 `LibraryChanged` 用 **`ItemsAdded` 数组**承载新增条目，不是单个 `Item`；`webhook.py` 只做 `.lower()`，下游 `notification_bot_webhook_event_service.py` 只匹配 `item.added` / `library.new` 子串。`itemadded` 与 `librarychanged` 都匹配不上，入库事件被静默丢弃，日志里连 `🔔 触发事件: item.added` 都不会打印。
- 已排除：Bark 配置 / 网络 / Device Key（实测推送 200）、事件总线订阅、`enable_library_notify` 总开关、白名单同步日志（审批工单触发，与入库无关）、「New Media Notification」（轮询计划任务的监控记录）。

### 改造：`app/domains/system/webhook_events.py`（新增纯函数模块）

1. **事件名归一化**：`EMBY_TO_INTERNAL` 把 `ItemAdded` / `LibraryItemAdded` → `item.added`、`LibraryChanged` → `library.new`、`PlaybackStart/Stop` → `playback.start/stop`、`AuthenticationSuccess` / `UserAuthenticated` → `login`、`ItemRemoved` / `ItemDeleted` → `remove`。查表前去掉空格 / 点 / 下划线 / 连字符，兼容 `Item Added` 等写法；`get_raw_event()` 兼容 `Event` / `event` / `EventType` 字段。已是内部格式的事件名（`library.new` 等）保持原样，避免归一化把点号打散后反而匹配不上。
2. **LibraryChanged 拆条**：`ItemsAdded` 数组逐条发布 `item.added`。字符串元素补成 `{"Item": {"Id": ...}}`；对象元素原样保留 `Type` / `SeriesId` / `Name`，利于下游剧集分组与季海报插件识别。只带 `Id` 的载荷由 `push_single_item()` 调 Emby API `/Items/{Id}` 补全明细，不会推出「未知影片」。拆条成功后不再发布原始 `library.new`（其载荷无单个 `Item`，下游无事可做）。
3. **双事件去重**：Emby 常同时勾选 `Item Added` 与 `Library Changed`，同一部片会来两次。除 `add_library_task()` 既有的队列内按 Id 去重外，新增 5 分钟进程内 `mark_item_seen()` 跨批次去重，避免收到两条 Bark / 两条用户端通知。
4. **未识别事件限流告警**：`warn_unknown_event_once()` 按事件名只告警一次，日志明确「未识别 Event=xxx（归一化=yyy）」，方便核对 Emby 实际发了什么，又不会被 `SessionStarted` 等高频事件刷屏。

`webhook.py::emby_webhook` 接入上述四步；`_get_webhook_token()` 与既有 Token 校验 / 违规客户端拦截 / 播放 IP 保存逻辑完全不变。

### 回归

- 新增 `tests/test_webhook_events.py`，47 项纯标准库断言全绿，已接入 CI 第一关：事件名映射（含旧格式不破坏、大小写与分隔符变体）、`ItemsAdded` 数组拆条（字符串 / 对象 / 脏数据 / 非数组 / 空）、双事件去重（含 TTL 到期与重置）、未识别事件限流，以及**端到端**：把 Emby 原生 `ItemAdded` / `LibraryChanged` 载荷送进 `handle_webhook_event()` 验证入库任务真实入队，并反向复现 1.9.7 缺陷（未归一化的 `itemadded` 不入队）。
- `tools/verify_patches.py` 新增「问题 14」断言组（映射表 / 拆条 / 去重 / 限流 / webhook.py 接线 / CI 接入）。
- 全量回归：`compileall`、`verify_patches`、`check_bark_channel_contract`、`test_bark_push`、`test_user_notify`、`test_tg_whitelist_sync`、`check_session_cookie_isolation`、`check_notification_bot_subscription`、`check-no-secrets`、模板内联 JS `node --check`、YAML 解析。

### 发布与验证

- 应用版本升级至 `1.9.8`；compose 默认锁定 `1.9.8`，`1.9.7` 仍保留，可用 `IMAGE_TAG=1.9.7` 回退；`request_app.js?v=1.9.8` 同步递增。
- 升级后验证清单：① Emby Webhooks 插件确认勾选 `Item Added` 与 `Library Changed`，回调 URL / Token 指向本服务；② 触发一次入库（或手动「扫描媒体库」扫进一部新片），容器日志应出现 `🔔 触发事件: item.added` 或 `LibraryChanged 拆分 N 条`；③ Bark 收到「新片入库」、用户端开启「新片入库」开关的用户也收到；④ 同一部片不会推两条；⑤ 若日志出现 `[Webhook] 未识别 Event=`，把该事件名反馈回来补进映射表。

## 1.9.7

集中处理 1.9.6 升级后反馈的第 3、4 项，并按新增要求给管理端两个通知设置浮窗补上真实 Bark 链路开关。系统设置页已删除的 Webhook / Bark 模块不再改动。

### 修复：新片入库 Bark 推送仍不可用（反馈第 3 项，对标企业微信链路）

**全链勘察结论**：Emby Webhook → 事件解析 → 入库队列 / 批处理 → `bus.publish("notify.library.new_item" / "new_episode")` → `NotificationBot` handler 这一段，企业微信与 Bark **完全共用**；两者只在 handler 内部分叉。企微由 `get_notify_channels("library_new")` → `_get_platform(channels)` → `send_photo(platform="wecom")` 驱动，Bark 则是 `_bark_notify_new_item()` → `bark_push.safe_push("library_new")` 的独立分支，由 `enabled` / `notify_library_new` 两个自身开关控制。既然企微能收到，上游链路基本存活，断点落在 Bark 分支的配置与开关上。

定位到三个真实断点：

1. **配置根本没落盘（主因）**：`templates/bot.html` 的 Bark 卡片字段不在顶部「保存配置」的 `/api/bot/settings` payload 里，只有卡片底部独立的「保存 Bark」按钮会提交 `/api/notify/channels_config`；而顶部保存成功后会调用 `init()` 重读并覆盖整个表单。用户「填好 Device Key → 只点顶部保存」的结果是配置被静默丢弃，运行期读到空 Key。
   - 修复：`saveBotBarkSettings()` 改为返回成败并支持静默模式，`saveConfig()` 在 `/api/bot/settings` 成功后 `await saveBotBarkSettings({silent: true})` 再 `init()`；失败时明确提示，不再伪装成全部保存成功。
2. **总控里的 `'bark'` 是后端不消费的死值**：1.9.5「推送场景总控」允许勾选 Bark 并写入 `library_notify_channels`，但 `_get_platform()` 只识别 `tg_bot` / `wecom`，Bark 分支也不读 `channels`，用户「勾了却不推」。
   - 修复：借鉴企微的 channels 驱动模型，`notification_settings.is_bark_channel_selected(notify_type)` 按通知类型精确判断；`bark_push.safe_push()` 通过 `_apply_channel_gate()` 把勾选折算为启用信号。按 `library_new` / `request_new` 分别映射，避免入库勾选串扰求片、或求片通知被重复触发。显式注入 `config` 的调用（回归测试 / 用户级推送）行为完全不变。
3. **跳过原因不可自证**：原先未启用 / 缺 Key / 地址非法只产生静默 INFO 或被上层吞掉。
   - 修复：`send()` 的三个跳过分支将具体原因写入日志，缺 Key 时直接提示「请在机器人助手 → Bark 推送保存，再点顶部保存配置」。

**配置边界澄清（按用户纠正）**：`.env` 里的 `WEBHOOK_TOKEN` 只是 Emby Webhook 回调 `?token=` 的**接口校验凭证**，属于「入库事件入口」；Bark 配置属于「推送出口」，只从 `config.json` 读写。二者没有任何配置耦合，不能把 Bark 不推送归因于 `.env`；本版同时**撤回**了上一轮误加的 `BARK_ENV_FIELDS` / `BARK_*` 读取逻辑，`get_bark_config()` 恢复为 1.9.6 行为，并在文件头写死该边界说明。

### 修复：用户端通知设置仍打不开（反馈第 4 项，回归页面内弹窗）

- **真根因**：不是 Alpine 作用域、也不是 JS 缓存版本号。`app/bootstrap/user_portal.py` 的 10308 用户端 wrapper 有路径白名单，1.9.6 新增的页面路径 `/user/notify_settings` 不在其中，被直接 403「非法越界，后台管理界面已被物理阻断」。
- **方案（按用户要求「不要单独打开一个页面」）**：新增 `templates/request/modal_notify.html`，纯原生 JS、零 Alpine / `x-teleport` / 外部 JS 依赖，随 HTML 一起下发，由 `request.html` include；「我的」页按钮改为 `onclick="epNotifyOpen()"` 页面内弹窗，与「媒体库设置 / 娱乐功能」同款交互。
- **布局（按用户要求）**：「媒体库设置 / 通知设置 / 娱乐功能」三按钮一排 `grid grid-cols-3`，与上方「我的心愿 / 我的报错 / 追剧更新」对齐。
- **兼容**：`templates/user_notify.html` 独立页删除；`GET /user/notify_settings` 改为 303 重定向到 `/request?tab=profile&notify=1`，弹窗脚本检测参数自动打开；白名单保留该路径放行，旧缓存 / 收藏链接不再 403。`request_app.js` 版本号递增至 `?v=1.9.7`。

### 新增：管理端两个通知设置浮窗的 Bark 链路开关（真开关）

- `notify_admin.CHANNEL_OPTIONS` 纳入 `bark`；`templates/requests_admin.html` 四类（工单提交 / 新追新 / 状态变更 / 用户报错）与 `templates/users.html` 三类（用户注册 / 用户登录 / 用户删除）各新增 `data-channel="bark"` 勾选，加载 / 保存沿用既有 `querySelectorAll` 通用收集，无需改 JS。
- `bark_push.py` 新增 `push_rule_notification()` / `safe_rule_push()`：按 `notify_rules` 对应行的 `enabled` 与 `channels` 精确判断是否推送，勾选即视为启用意图，Telegram HTML 文案自动去标签；异常全量吞掉，不影响业务主流程。
- 后端 6 个触发点接入：`feedback_router.py`、`registration_router.py`、`delete_router.py`、`notification_bot_user_login_service.py`、`update_router.py`、`management_router.py`。求片提交（`submit_router.py`）复用既有 `safe_push("request_new")` 的 channels 门控，**不重复接入以免双推**。

### 回归

- `tests/test_bark_push.py` 新增第 7 节行为回归：总控未勾不推 / 勾了真发请求且携带正确 Key / 入库勾选不串扰求片 / 浮窗未勾跳过、勾了真推、规则禁用不推 / HTML 去标签 / 注入 config 时不受门控影响。93 项全绿。
- `tools/check_bark_channel_contract.py` 扩到 48 项：保存链路、channels 门控、诊断日志、配置边界、7 个勾选、6 个触发点真实消费，全绿。
- `tools/verify_patches.py` 断言升级到 1.9.7 语义（弹窗 / 三按钮对齐 / 独立页删除 / 303 兼容 / 白名单 / Bark 真开关 / 版本）。
- `compileall`、模板内联 JS `node --check`、`test_user_notify.py`、`test_tg_whitelist_sync.py`、`check_notification_bot_subscription.py`、`check_session_cookie_isolation.py`、`check-no-secrets.py` 全绿。

### 发布与验证

- 应用版本升级至 `1.9.7`；compose 默认锁定 `1.9.7`，`1.9.6` 仍保留，可用 `IMAGE_TAG=1.9.6` 回退。
- 升级后验证清单：① 机器人助手页填 Device Key 后**只点顶部「保存配置」**，重进页面应显示掩码值（说明已落盘）；② 在「推送场景总控」勾选 Bark 渠道或卡片「启用 Bark 推送」，触发 Emby 新片入库，Bark 应收到通知；③ 未收到时查系统日志「[Bark] 未推送原因」定位；④ 用户端「我的 → 通知设置」应页面内弹窗可保存，PC / 手机一致；⑤ 工单大厅与用户管理通知设置勾选 Bark 后，对应事件应收到管理员侧 Bark 提醒。

## 1.9.6

集中处理 1.9.5 用户反馈的 4 项问题：用户端通知设置彻底重构为独立页面、系统设置删除 Bark 与 Webhook 两个模块、机器人助手保存报 500 的根因修复、系统日志 500 堆栈清除。

### 修复：机器人助手保存报 500（`BotSettingsModel` 缺 `webhook_token` 字段）
- **根因**：1.9.5 机器人助手页保存时提交 `webhook_token` 字段，但 `app/schemas/models.py` 的 `BotSettingsModel` 未定义该字段，路由 `data.webhook_token` 取不到 → `AttributeError` 500，且前端每次保存循环触发，系统日志被堆栈刷屏。
- **修复**：`BotSettingsModel` 新增 `webhook_token: Optional[str] = ""`；保存链路沿用 `should_update_sensitive_bot_setting`（环境变量控制 / 空值 / 脱敏值均不覆盖），与既有敏感字段同一套保护。

### 重构：用户端「通知设置」独立页面（彻底解决点击无反应）
- **背景**：1.9.4 修 Alpine 嵌套作用域、1.9.5 修 JS 缓存版本号，用户侧两次反馈仍无反应；连续两轮修复无效后按用户要求**彻底重构**，不再依赖 Alpine。
- **方案**：新增独立页面 `GET /user/notify_settings`（`templates/user_notify.html`，纯 HTML + 原生 JS + Tailwind CDN，零 Alpine 零作用域零缓存问题），未登录跳 `/request_login`；`tab_profile.html` 的「通知设置」按钮由 `@click` 改为 `<a href>` 直达；删除 `tab_profile.html` 的 teleport 弹窗与 `request_app.js` 中的全部 notify state / 方法。
- **不变**：接口契约（`GET/POST /api/user/notify_settings`）、双渠道（Bark / Webhook 二选一）、四类内容开关（新片入库 / 管理员消息 / 全站公告 / 心愿清单）。
- `request.html` 的 `request_app.js?v=1.9.5` 递增为 `?v=1.9.6`。

### 删除：系统设置的 Webhook 与 Bark 两个模块（按用户要求）
- 「系统神经感知 (Webhook)」引导卡与「系统级 Bark 推送」卡片**整卡删除**（含 `collectBarkSettings` / `loadBarkConfig` / `saveBarkSettings` / `testBarkSettings` 四函数与 `loadBarkConfig()` 初始化调用），配置入口统一收敛至「通知与机器人 → 推送场景总控」。

### 优化：.env 控制 Webhook Token 时的输入框体验
- 环境变量控制时输入框**不再禁用**：可粘贴 .env 原值，仅用于本地生成「带 `?token=` 的完整回调地址」；保存时后端检测 env 源自动忽略，不会覆盖 .env 配置，界面保存边界不变。

### 回归
- `py_compile` / `node --check`（模板内联脚本全量语法校验）/ `verify_patches` / `check_bark_channel_contract` / `test_user_notify` / `check_notification_bot_subscription` 全绿。
- `grep` 验证：`request_app.js` 与 `tab_profile.html` 无任何 notify 残留；`settings.html` 无 `settings-bark-*` 残留。

### 发布与验证
- 应用版本升级至 `1.9.6`；compose 默认锁定 `1.9.6`，`1.9.5` 仍保留，可用 `IMAGE_TAG=1.9.5` 回退。
- 升级后验证清单：① 用户端「我的 → 通知设置」跳转独立页面并可保存；② 机器人助手页保存配置不再 500；③ 系统设置页不再有 Webhook / Bark 卡片；④ .env 配了 WEBHOOK_TOKEN 时可粘贴原值生成完整地址。

## 1.9.5

集中处理 1.9.4 用户反馈的 5 项问题：用户端通知设置入口仍无反应、Bark 配置入口位置、Webhook 双入口重复且缺 Token、入库通知可达性验证、工单大厅移动端批量栏显示不完整。

### 修复：用户端「通知设置」点击仍无反应（缓存破坏）
- **根因**：1.9.4 已将通知设置 state / 方法正确迁移至 `requestApp` 主作用域（与可用的「媒体库设置」弹窗同款模式），但 `request.html` 引用 `request_app.js?v=1.3.53` 的**查询版本号自项目独立起从未更新**——浏览器与反代持续使用缓存的旧 JS（其中没有 `loadNotifySettings`），新 HTML 配旧 JS 再次 `is not defined`，PC 与手机双双中招；而旧 JS 里本就存在「媒体库设置」方法，所以只有通知设置失灵。
- **修复**：`request_app.js` 引用升级为 `?v=1.9.5`，镜像升级后浏览器必然拉取新文件。**后续每次发版必须同步递增该版本号**（已写入发版检查项）。
- 升级后如仍无反应：硬刷新（Ctrl/Cmd+Shift+R）或清理站点缓存后重试。

### 新增：推送场景总控增加 Bark 推送渠道与配置卡片（需求 1）
- 「通知与机器人 → 推送场景总控」的「通知渠道」勾选组新增 **Bark 推送** 选项（与 TG 机器人 / TG 频道 / 企业微信并列；`_get_platform` 只识别 tg_bot / wecom，新增值不影响既有渠道逻辑）。
- 同页新增 **「Bark 推送」配置卡片**：启用总开关、服务器地址、Device Key（掩码显示，保存不覆盖已存密钥）、「新片入库通知 / 新的求片通知」独立开关、提示音、分组、「保存 Bark」「发送测试」。保存走 `POST /api/notify/channels_config`（只提交 `bark` 节点，不覆盖 TG / 企微配置），测试走 `POST /api/notify/bark_test`。
- 系统设置页原「系统级 Bark 推送」卡片保留，两处读写同一份配置。

### 整合：Webhook 入口统一至机器人助手并完善 Token（需求 2）
- **去重**：系统设置页「系统神经感知 (Webhook)」的 Token 输入框与地址预览移除，改为**引导卡**（说明 + 一键跳转「通知与机器人」），避免两个入口重复维护。
- **完善 Token**：机器人助手「推送场景总控 → 新资源入库通知」的 Webhook 区域现在包含：**带 `?token=` 的完整回调地址**（可直接复制，脱敏值不拼入地址，显示「你的Token」占位并实时同步）、**接口验证 Token 输入框**（脱敏回填、环境变量控制时只读禁用、修改后地址实时更新）、复制按钮（含 `execCommand` 降级，适配内网 http 非安全上下文）。
- **保存链路**：`POST /api/bot/settings` 新增 `webhook_token` 字段保存（复用 `should_update_sensitive_bot_setting`：环境变量控制 / 空值 / 脱敏值均不覆盖），与系统设置页同一份配置、同一套保护。

### 优化：工单大厅移动端批量操作栏横滑（需求 5）
- 求片 / 报错两个底部悬浮批量栏：新增 `-webkit-overflow-scrolling: touch`（iOS 惯性滚动）与**鼠标 / 触摸拖拽横滑**（`startBatchDrag` / `moveBatchDrag` / `endBatchDrag`，拖拽后吞掉误触 click），按钮多时左右拖动浏览，避免「手动接单 / 拒绝」等按钮显示不完整；触摸时不阻止原生滚动，双轨互补。

### 回归
- `verify_patches.py` 新增「问题 12」断言组（12 条）：JS 版本号、notify 作用域落位、Bark 渠道与卡片、Webhook Token 保存、批量栏拖拽、CHANGELOG 等。
- `py_compile` / `node --check`（三个模板内联脚本全量语法校验）/ `test_bark_push` 84/0 / `test_user_notify` 12/0 / `check_notification_bot_subscription` 29/0 / `test_tg_whitelist_sync` 102/0 全绿。

### 发布与验证
- 应用版本升级至 `1.9.5`；compose 默认锁定 `1.9.5`，`1.9.4` 仍保留，可用 `IMAGE_TAG=1.9.4` 回退。
- 升级后验证清单：① 用户端「我的 → 通知设置」正常打开弹窗（必要时硬刷新）；② 机器人助手页可见 Bark 推送卡片并保存 / 测试通过；③ Webhook 区域复制完整地址（含 Token）填入 Emby；④ 手机端工单大厅批量勾选后左右拖动批量栏。

## 1.9.4

修复 1.9.3 用户反馈的两个问题：① 用户端口「我的 → 通知设置」入口点击无反应；② 新片入库通知未推送的可观测性缺失。同时加固服务启动流程。

### 修复：通知设置入口点击无反应（Alpine 作用域缺陷）
- **根因**：通知设置的 state（`notifySettings*`）与三个方法（`loadNotifySettings` / `saveNotifySettings` / `setNotifyChannel`）原先定义在 `tab_profile.html` 头像卡的**嵌套 `x-data`** 中，而入口按钮（`@click="loadNotifySettings()"`）与 `x-teleport` 弹窗都在父级 `requestApp` 作用域。嵌套作用域的方法对父作用域不可见，按钮点击时 `loadNotifySettings` 未定义，弹窗也读不到同一份 flag，表现为「点击无反应」。
- **修复**：将全部通知设置 state 与方法迁移至 `static/js/request_app.js` 的 `Alpine.data('requestApp', ...)` 主组件（state 位于 `entertainmentModalOpen` 附近，方法位于 `loadLibrarySettings()` 之前）；`tab_profile.html` 仅保留按钮引用与弹窗结构，不再定义任何方法。
- **未改动**：通知设置的接口契约（`GET/POST /api/user/notify_settings`）、弹窗 UI 结构、四类开关与双渠道逻辑。

### 增强：入库通知全链诊断日志 + 启动加固（定位「新片入库未推送」）
- **全链日志**：`webhook_event_service` 入队后记录 `[Webhook] 入库任务已入队`；两个入库 handler（`library_new_item` / `library_new_episode`）入口记录 `[入库通知] 收到新片入库事件`；Bark 发送结果**总是记录**（跳过时记录原因，成功时记录「推送成功」）。链路任一段断裂均可从 `docker logs` 直接定位。
- **启动加固**：`service_registry.start_all` 增加 per-service try/except，单个服务启动异常仅记 `error` 并继续启动后续服务，避免「一个服务抛错静默杀死 notifications 事件订阅」这类连锁失败。
- **排查提示**：Emby Webhook 的「测试」事件返回成功仅代表端点与 token 正常，`test` 类事件**不会**产生通知（设计如此）；请添加真实影视触发 `item.added`，再按 `docker logs` 全链日志定位，并核对系统级 Bark 的总开关 / 「新片入库通知」开关 / Device Key / 服务器地址。

### 回归
- `test_bark_push` 84/0、`test_user_notify` 12/0、`check_notification_bot_subscription` 29/0、`verify_patches` 129/0 全绿；`node --check static/js/request_app.js` 通过；`check-no-secrets.py` 0 findings。
- `grep` 验证：notify state/method 仅在 `request_app.js` 定义，`tab_profile.html` 仅剩按钮与弹窗引用。

### 发布与验证
- 应用版本升级至 `1.9.4`；compose 默认锁定 `1.9.4`，`1.9.3` 仍保留，可用 `IMAGE_TAG=1.9.3` 回退。
- 升级后进入用户端口「我的 → 通知设置」应正常打开弹窗；添加真实影视后按日志链确认入库通知。

## 1.9.3

本版本聚焦「用户端口通知设置」「工单大厅交互优化」「Bark 入库文案统一」与文档重构，全部为增量能力，不改动既有接口与系统级推送逻辑。

### 新增功能
- **用户端口「通知设置」（需求 5）**：用户在本机「我的 → 通知设置」自助配置，与管理端系统级 Bark **互不干预**。支持两种渠道自选（用户自定义 Bark 链接 / 用户自定义 Webhook）；四类内容开关：① 新片入库通知 ② 管理员发送的消息 ③ 全站公告 ④ 心愿清单状态变动（有变动才推，无变动不推）。推送内容**不附带网址**，任意单用户失败被吞掉、不影响主流程。
- **工单大厅拒绝按钮 + 移动端优化（需求 4）**：单条审批在「推给 MP / 手动接单」之后、「影巢」之前新增**拒绝**按钮；移动端批量悬浮栏左移并增加底部留白，修复勾选后按钮被遮挡；各按钮功能联动不变。
- **Bark 新片入库正文改走「自定义通知模板」（需求 3）**：`library_new_item` / `library_new_episode` 正文复用 `notify_template` 插件渲染，与 TG / 企业微信走同一套风格模板；并修复「仅启用 Bark 时新片入库不推送」的遗留问题（含 `docs/bark_library_push_troubleshooting.md` 容器排查报告）。
- **关于与支持页链接修正（需求 2）**：GitHub 跳转链接改成本项目仓库；官方 TG 频道 / 交流群组链接置空（展示「暂未开放」）。
- **README 重构（需求 1）**：按原项目 4 段式重写——（1）继承内容 + 两源项目图标跳转链接 + 鸣谢；（2）本项目新增功能；（3）使用说明；（4）MIT + 附加条款。

### 回归
- 用户通知设置链路：schema 注册 `users_meta.user_notify_settings` → DAO（4 个函数）→ `user_notify_service`（推送 / 广播 / 心愿）→ `user_notify_router`（`GET/POST /api/user/notify_settings`）→ 路由注册 → 用户端口白名单 → 前端「通知设置」弹窗；四个触发点（消息 / 公告 / 心愿变动 / 入库）已接线。
- `py_compile` 校验 5 个触发文件全绿；`verify_patches.py` 版本断言更新至 1.9.3。

### 发布与验证
- 应用版本升级至 `1.9.3`；compose 默认锁定 `1.9.3`，历史版本仍可通过 `IMAGE_TAG` 回退。

## 1.9.2

修复「只启用系统级 Bark、未配置 TG Bot / 企业微信时，新片入库不触发 Bark 通知」的问题。1.9.1 及更早版本均受影响；求片 Bark 通知与工单自动流转不受影响，仍正常。

### 根因：事件订阅被渠道凭证误门控
- `NotificationBot.start()` 原有一行 `if not get_bot_tg_token() and not get_wecom_corpid(): return`，在 `_subscribe_events()` **之前**就退出。系统级 Bark 是 1.9.0 新增的独立通道，不依赖任何 TG / 企微配置，但它的入库通知 handler 挂在 `notify.library.new_item` / `notify.library.new_episode` 两个事件上，只有 `NotificationBot` 成功启动并订阅后才会执行。
- 因此只配置 Bark 时，这两个事件**没有任何订阅者**。`EventBus.publish()` 对无订阅者是静默空转（不抛异常、不记日志），入库通知被无声丢弃，排查时看不到任何错误。
- 「工单大厅流转已入库」是正常的：`_auto_finish_request()` 由 `SystemDaemon` 的入库轮询链路在 `push_single_item()` / `push_episode_group()` 里直接调用，与后续事件订阅不是同一层；`process_library_group()` 也没有「入库通知总开关」前置过滤。
- 「求片 Bark 正常」也是正常的：`submit_router.py` 直接调用 `safe_push("request_new", ...)`，不经过事件总线。

### 修复
- 事件订阅与 `running` 标记改为**无条件建立**，不再受 TG / 企微凭证影响；`notify.user.login` / `notify.playback.*` / `notify.item.deleted` / `notify.daily_report` 等内部事件订阅一并恢复正常。
- 外部渠道动作改为**精确门控**：`has_tg` 时才注册 TG 命令并启动 `notification-bot-polling` 线程，`has_wecom` 时才下发企微菜单。无凭证时不产生任何无效外部调用；各 handler 内的 `send_message` / `send_photo` 本身也有 token 空值判断，双重保险。
- 启动日志追加 `(event subscriptions active, tg=..., wecom=...)`，便于现场确认订阅状态。
- **未改动** Bark 发送模块、配置层、求片入口、Webhook 兼容逻辑与任何既有接口；Bark handler 仍位于「入库通知总开关」判断之前，只由自身 `enabled` / `notify_library_new` 两个开关控制。

### 回归
- 新增 `tools/check_notification_bot_subscription.py`（纯标准库，29 条断言）并接入 CI：用 AST 从真实源码抽出 `start` / `_subscribe_events` / 两个入库 handler，在受控命名空间执行，覆盖「仅 Bark / 仅 TG / 仅企微 / 全部配置 / 重复 start / 取消订阅」六种场景，并把「publish 入库事件 → handler → Bark」整条链闭环验证（含把入库通知总开关置为 False 时 Bark 仍触发）。
- `verify_patches.py` 新增「问题 11」静态断言；既有 1.7.0 / 1.8.0 / 1.9.0 / 1.9.1 全部回归关卡保持全绿。

### 发布与验证
- 应用版本升级至 `1.9.2`；compose 默认锁定 `1.9.2`，`1.9.1` 仍保留，可用 `IMAGE_TAG=1.9.1` 回退。
- 升级后无需重新配置 Bark，也无需配置 TG / 企业微信。触发一次真实新片入库，确认收到「新片入库」推送；求片推送与工单流转应保持不变。

## 1.9.1

修复 1.9.0 的 Bark 配置「找不到入口」问题。按部署方要求，**不新增独立菜单板块**，改为把 Bark 配置直接内置进「系统设置」页。

### 修复：Bark 配置入口
- **根因**：`/notify_admin` 通知管理页一直存在（含 Bark 卡片），但上游从未把它挂到侧边栏导航，权限映射 `PERM_NAV_MAP` 里也没有它；用户按「通知管理 → 推送渠道」找时，连这个板块都看不到。属于「后端能力已具备、界面无入口」，不是 Bark 功能缺陷，也不是容器版本问题。
- 现于「系统设置」页新增 **「系统级 Bark 推送」卡片**：总开关、服务器地址、Device Key、新片入库/新的求片两个独立开关、提示音与分组、「保存 Bark」「测试推送」两个按钮。保存与测试都走 `/api/notify/channels_config` / `/api/notify/bark_test`，**只提交 `bark` 节点**，不混入顶部「保存配置」的 `/api/settings` 模型，也不会覆盖 TG / 企业微信渠道配置。
- Device Key 读取时掩码显示；保存成功后自动重新拉取，让输入框显示后端下发的掩码值；「测试推送」可用当前表单里尚未保存的服务器与 Key 直接验证。
- 卡片底部保留「通知规则更多设置 →」链接，指向完整的 `/notify_admin` 页（通知规则矩阵、TG 频道、发送历史）；侧边栏导航保持 1.9.0 原样，**未新增任何菜单项**。

### 立即可用（无需升级）
- 1.9.0 用户可直接访问 `http://<内网IP>:10307/notify_admin` → 「推送渠道」Tab → 第 4 张「系统级 Bark 推送」卡片配置，功能与设置页卡片完全等价（同一套接口与配置）。
- 1.9.1 只是把入口前移到系统设置页，不改变 Bark 推送逻辑、Webhook 兼容逻辑或任何既有接口。

### 发布
- 应用版本升级至 `1.9.1`；compose 默认锁定 `1.9.1`，已发布版本增加 `1.9.1`，仍可用 `IMAGE_TAG` 切换历史版本。
- `verify_patches.py` 新增「问题 10」静态断言：设置页 Bark 卡片字段齐全、独立保存/测试函数存在、只提交 `bark` 节点、初始化自动加载配置、侧边栏未新增独立板块、版本与 CHANGELOG 同步。

## 1.9.0

本次发布新增**系统级 Bark 推送入口**（新片入库 / 新的求片两类消息），并修复 **Emby Webhook 入口因只支持 `?token=` URL 而不可用** 的问题。建议升级前备份配置与数据库。

### 新增：系统级 Bark 推送入口
- Bark 是**系统级独立通道**，不加入 `notify_rules` 的渠道勾选列表，不受原有「通知总开关 / 入库通知总开关」约束；配置集中在「通知管理 → 推送渠道」的 Bark 卡片：服务器、Device Key、总开关、新片入库开关、新的求片开关、铃声与分组。
- 两类消息覆盖：`push_new_library_item()` 处理电影入库与剧集新集入库；`push_new_request()` 处理用户提交求片。管理端提供「测试推送」按钮（`POST /api/notify/bark_test`），绕过开关验证服务器与 Key 是否可用。
- 统一发送模块 `app/domains/notifications/bark_push.py` 只用标准库，POST `{server}/push`；支持自建 Bark 服务器。事件侧统一走 `safe_push()`，**任何异常都吞掉只记日志，绝不阻断求片提交 / 入库通知主流程**。
- 采用可注入 provider（`bark_push_provider`），边界测试可替换，既有 `set_dependency_providers()` 调用无需改动。

### 安全与可靠性
- Device Key 属敏感信息：管理端读取一律掩码，运行期才读原文；保存时回填的掩码值**不会覆盖**已存的真实 Key；推送返回值与日志中的 payload **不含** `device_key`。
- 未启用 / 未配置 Key / 服务器地址非法时直接跳过不发请求；标题、正文、跳转 URL 均做长度截断；HTTP 非 2xx、Bark 返回 `code != 200`、网络异常均返回结构化失败结果而不抛异常。

### 修复：Emby Webhook 入口不可用
- **根因**：Emby Webhook 插件只能配置一个回调 URL，**无法附带自定义请求头**；而服务端 `_get_webhook_token()` 默认只读 `X-Webhook-Token`，且 `Dockerfile` / `compose` 默认 `WEBHOOK_ALLOW_QUERY_TOKEN=0`，设置页又只提供纯路径并推荐 Header 方式，导致按插件能力配置的 `?token=` 地址一律 401。
- 现改为**默认兼容** `http://<host>:10307/api/v1/webhook?token=<Token>`；`X-Webhook-Token` 请求头**仍优先读取**。若反代能注入请求头且需收紧访问日志，可显式设 `WEBHOOK_ALLOW_QUERY_TOKEN=0` 回到严格 Header-only 模式。
- 设置页「推送目标地址」改为生成 Emby 可直接使用的完整地址并支持一键复制；因后端出于安全只回传**脱敏** Token，界面用占位符「你的Token」兜底（绝不把掩码值拼进 URL，避免复制到 Emby 后持续 401/403），用户手动填入原始值时会实时拼出可直接复制的地址。系统设置接口同步返回 URL 模板与替换提示。
- 后端**仍不向前端回传明文 Webhook Token**（`webhook_url` 保持纯路径、凭证字段一律脱敏），F12 泄露防护未被削弱。

### 部署注意
- `WEBHOOK_ALLOW_QUERY_TOKEN` 默认值由 `0` 改为 `1`（`Dockerfile` / `compose` / `.env.example` 已同步）。本部署的 Webhook 是 **Emby Server → EMBYPULSE 的内网直连**，服务端直接发起，不经浏览器 / CDN / 公网反代，因此 URL 中的 Token 不会进入浏览器历史、Referer 或公网反代日志，只剩 EMBYPULSE 本机访问日志（内网本地）；内网拓扑下无需为此额外加固。仅当把 Webhook 改走公网反代或把本机日志外发时，才需收紧日志采集或设回 `0`。
- 注意设置页的 URL 预览基于「当前访问管理端的 host」生成：若从公网域名打开设置页，复制到 Emby 后请把 host 换成实际内网 IP，避免把公网域名写进内网回调。
- 升级到 1.9.0 后，Emby Webhook 插件里把回调地址改为带 `?token=` 的完整 URL 即可，无需再配置请求头。

### 发布
- 应用版本升级至 `1.9.0`；compose 默认锁定 `1.9.0`，已发布版本 `1.5.0` / `1.6.0` / `1.7.0` / `1.8.0` / `1.9.0`，可用 `IMAGE_TAG` 切换（回退 1.5.0 用 `IMAGE_TAG=v1.5.0`）。
- CI 新增「校验系统级 Bark 推送与 Webhook token 兼容」关卡（纯标准库行为回归 84 条）；`verify_patches.py` 新增「问题 9」静态断言，并把「问题 3/5」断言从「默认拒绝 URL token」更新为「默认兼容但不回传明文」。

## 1.8.0

本次发布新增「工单删除 → 同步移除 TgtoDrive 对应监控关键词」的删单联动。插件 B（TgtoDrive 侧完结清理）保持不变，两条清理路径互不替代。建议升级前备份配置与数据库。

### 新增
- **删单联动**：在 EMBYPULSE 工单大厅删除求片后，异步移除 TgtoDrive 频道监控里该工单对应的 `kw_<tmdb>_<season>` 规则。删除请求参数自带 `(tmdb_id, season)`，无需反查 DAO；批量删除同样覆盖，重复候选自动去重。仅 `delete` 动作触发，审批/接单/拒绝等不受影响；联动失败只记日志，绝不阻断工单删除主流程。
- **只删目标、完整保留其它规则**：移除时按目标场景精确定位规则，用户自建规则（`tmdb_*` 等）、其它工单的 `kw_` 规则、其它场景规则一律不动；同步信封内所有规则副本，清除三类陈旧正则，写入体沿用 `write_mode`。

### 安全护栏
- GET 失败 / 返回空配置(404) / 结构无法定位目标场景 → 中止，**绝不 POST**（避免把现有关键词整体清空）。
- 删除场景**不从零创建场景**；未命中任何目标规则时不 POST（零净改动）。
- client 层二次过滤：只接受 `kw_` 前缀 id，非本插件规则一律跳过。
- 全局 `dry_run` 生效：删单联动只记录、连 GET 都不发起，便于首次验证。

### 为什么不由插件 B 做
- 插件 B 按「影视已入库 Emby」判定完结并周期清理，**不感知 EMBYPULSE 工单删除事件**；工单删除后 DAO 行消失，插件 B 下次运行无从判断该规则该删还是该留。因此联动由 EMBYPULSE 侧主动调用 TgtoDrive OpenAPI 完成。插件 B v1.4.0 不做改动。

### 修复
- 补齐 `management_router` 模块级 `logger`：此前 `_trigger_tg_whitelist_push` / `_run_tg_whitelist_push` 的异常分支引用了未绑定的全局 `logger`，一旦获取插件实例失败或推送抛异常就会触发 `NameError`。现统一定义，推送与删单联动钩子均可正常记日志。

### 发布
- 应用版本升级至 `1.8.0`，插件 A 版本 `2.2.0`；compose 默认锁定 `1.8.0`，已发布版本 `1.5.0` / `1.6.0` / `1.7.0` / `1.8.0`，可用 `IMAGE_TAG` 切换（回退 1.5.0 用 `IMAGE_TAG=v1.5.0`）。
- CI 新增「校验求片同步插件（推送 + 删单联动回归）」关卡，直接运行改为按 `__file__` 推导路径的 `tests/test_tg_whitelist_sync.py`（102 条断言）；`verify_patches.py` 新增「问题 8」接线静态断言（共 72 条）。

## 1.7.0

本次发布依据 TgtoDrive **真实接口响应样本**定稿了求片推送的规则字段契约，并修复管理端(10307)与用户端(10308)互相踢下线的问题。建议升级前备份配置与数据库。

### 修复
- **求片推送覆盖频道监控关键词**：查明根因是 `GET /api/visual-filter/config` 返回的是信封结构（`rules` 位于 `current` / `scenes[场景]` 内，**顶层没有 rules**），旧逻辑读顶层 `rules` 恒为空，合并时把「只写新规则」当成全量，冲掉了用户已有关键词。现按插件配置的**目标场景**精确定位 `scenes[目标场景].rules`，不再误取「页面当前选中场景」的 `current`，避免串场景；写回时同步信封内所有规则副本。
- **必包含关键词未传递给 TgtoDrive**：字段名按真实响应样本定稿为 `required_keyword`（旧推断 `must_include` 后端不识别、直接丢弃），画面效果定稿为 `effect`（旧推断 `picture_effect` 同样无效），年份/`tmdb_id` 改为字符串。并补齐 `include_required_keyword` / `include_year` 布尔开关 —— 样本中插件写入的 `kw_297253_1` 规则 `required_keyword:""` + `include_required_keyword:false`，实证「只写值不开开关」后端仍不会把必含条件编进正则。规则对象补齐 `rule_type` / `source` / `keyword_groups_any` 等结构化字段。
- **清理陈旧正则**：`raw_regex` / `advanced_raw_regex` 之外，新增清除 `meta_raw_regex`，确保后端按 `rules` 重新编译。
- **10307/10308 两端口互踢**：两端同主机同域，浏览器 Cookie 不区分端口，共用 `session_id` 会互相覆盖；且登录/登出的 `session.clear()` 会删除共享会话行并轮换 id，另一端随即掉线。现用户端 wrapper 在 ASGI scope 打权威标记（不受 `Host` / `X-Forwarded-*` 伪造），会话中间件据此让用户端使用独立 Cookie `ep_user_session`，两端各自持有独立会话行，登录、登出、闲置均互不影响。

### 兼容与配置
- 旧版本手填的错误字段名 `must_include` / `picture_effect` 会在推送时**自动纠正**为真实字段名，无需手动改配置；仍可用插件配置「必含字段名」「必含开关字段名」覆盖。
- 新增插件配置「写入体形态 `write_mode`」：默认 `flat`（把目标场景配置提取成扁平体写回，不回传 `success` / `scene_labels` 等只读信封字段）。若现场后端 POST 返回 400 或规则不生效，切到 `envelope` 原样回写整个 GET 信封。该项属部署侧假设，建议先开 `dry_run` 并做一次真机推送验证。
- 升级后用户端(10308)因 Cookie 名变更需**重新登录一次**（管理端 10307 不受影响），属预期的一次性行为。

### 发布
- 应用版本升级至 `1.7.0`，插件 A 版本 `2.1.0`；compose 默认锁定 `1.7.0`，已发布版本 `1.5.0` / `1.6.0` / `1.7.0`，可用 `IMAGE_TAG` 切换。
- CI 新增「校验 10307/10308 会话 Cookie 隔离」关卡（真实执行会话中间件 dispatch，纯标准库、不依赖数据库）；补丁断言同步覆盖真实字段契约。

## 1.6.0

本次发布修复求片同步插件的推送覆盖/字段丢失问题，并补齐版本发布流程，建议升级前备份配置与数据库。

### 修复
- 求片同步推送改为「读-合并-写」安全护栏：当 TgtoDrive 返回结构无法解析时**中止推送**，避免误把现有频道监控关键词整体覆盖（仅增量追加本插件写入的 kw_ 规则）。
- 必包含关键词的字段名改为可配置（插件配置「必含字段名」，默认 `must_include`）；TgtoDrive 实际字段名未知时无需改代码即可修正，解决必含关键词未传递的问题。
  - ⚠️ 注：`must_include` 系当时的 UI 推断，**已在 1.7.0 依据真实响应样本证实为错误**，真实字段名为 `required_keyword`。请升级到 1.7.0；旧配置会自动纠正。
- 会话空闲/绝对超时与 JWT 有效期改为环境变量可调（`SESSION_IDLE_HOURS` / `SESSION_ABSOLUTE_HOURS` / `JWT_EXPIRE_HOURS`），并放宽会话 Cookie 的 SameSite 为 `lax`，避免同站导航偶发掉登录。

### 发布
- 应用版本升级至 `1.6.0`；CI 支持按 `v*` tag 构建带版本号的镜像标签，并在打 tag 时自动生成 GitHub Release；用户可在 compose 中通过 `IMAGE_TAG` 锁定历史/最新版本。

## 1.5.0

本次发布主要完成后端架构重整和稳定性加固，核心使用体验保持一致。生产环境升级前建议先备份配置和数据库。

### 优化

- 后端按业务域重组，用户、通知、播放、媒体请求、报表、系统、插件等模块边界更清晰。
- 管理机器人和用户机器人拆分为独立模块，降低命令、回调和后台任务之间的互相影响。
- 数据库初始化和表结构维护统一走 Schema 注册机制，减少升级时遗漏初始化逻辑的风险。
- 启动、定时任务、长轮询和插件调度等生命周期处理更集中，提升重启和停止时的稳定性。
- 外部请求和配置读取收口到统一边界，便于排查媒体服务器、TMDB、通知渠道和网络请求问题。

### 质量

- 补充大量边界与回归测试，覆盖用户管理、通知机器人、播放统计、媒体请求、报表、插件和系统接口。
- 应用版本升级至 `1.5.0`，Docker 构建配置同步更新。

### 注意

- 建议先在测试环境验证后再用于生产环境。

## 1.4.6

本次更新聚焦 Webhook Token 兼容性、管理权限校验和接入文档优化，建议所有用户升级。

### 优化

- Webhook 接入文档改为优先推荐通过请求头传递 Token，避免密钥出现在 URL、反向代理日志或浏览器历史记录中。
- 补充 Webhook Token Header 的配置说明，降低新环境接入和排查成本。

### 修复

- 收紧管理接口管理员权限校验，避免非管理员账号访问管理侧敏感操作。
- 禁止无密码 Emby 用户通过请求门户登录，避免绕过账号密码校验。
- 修复部分 Emby/Jellyfin Webhook 只能通过 URL 参数传递 Token 时无法正常鉴权的问题。
- 增加 Webhook URL token 参数回归用例，避免后续兼容性回退。

## 1.4.5

本次更新以稳定性和性能优化为主，建议所有用户升级。

### 新增

- Telegram 管理机器人新增 `/whois` 查询，可快速查看 Telegram 用户绑定的 Emby 账号。
- 新增系统性能观测接口，便于排查慢查询、缓存和线程占用问题。

### 优化

- 优化仪表盘、观影报告、海报和图片代理逻辑，减少大数据量场景下的内存占用和后台查询压力。
- 仪表盘缓存改为按管理员、用户分别隔离，避免不同访问身份互相影响。
- 用户社区缓存增加容量限制和刷新互斥，降低高频访问时的资源消耗。

### 修复

- 修复 Telegram 用户机器人提交求片后，部分环境下管理员通知未正常发送的问题。
- 修复求片审核状态同步、用户机器人菜单返回按钮缺失等交互问题。
- 修复观影报告封面缺失、剧集统计不准确等问题。
- 修复积分红包、打劫、商城等互动功能中的异常情况。
- 修复邀请码注册、用户社区注册在部分数据库返回格式下报错的问题。

### 验证

- 已通过应用编译检查和回归测试。
