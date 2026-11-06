# 🛡️ emby-pulse 安全加固独立版（基于 EmbyPulse）

<div align="center">
  <img src="static/img/logo-light.png" alt="EmbyPulse Logo" width="180" />

  <h3>Emby 服务器的高级伴侣：数据洞察、全能管理与下载自动化助手 · 安全加固独立发行版</h3>

  <p align="center">
    <a href="https://github.com/Abson2/emby-pulse"><img src="static/img/github-mark.svg" alt="GitHub" width="16" height="16" valign="middle" /> 本项目仓库 · Abson2/emby-pulse</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/zeyu8023/emby-pulse"><img src="static/img/github-mark.svg" alt="GitHub" width="16" height="16" valign="middle" /> 源项目 1 · zeyu8023/emby-pulse</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/amlkiller/emby-pulse"><img src="static/img/github-mark.svg" alt="GitHub" width="16" height="16" valign="middle" /> 源项目 2 · amlkiller/emby-pulse</a>
  </p>
</div>

> **镜像**：`ghcr.io/abson2/emby-pulse-hardened:latest`（独立包名，早期 `ghcr.io/abson2/emby-pulse` 为已废弃孤儿包，请勿拉取）。
>
> **仓库性质**：干净的**独立公开仓库**，**不是 fork、没有 upstream remote、不带上游任何提交历史**。内容是在上游原代码基础上做完善的完整项目源码——源码、Dockerfile、compose、CI 全部自持，可直接 `git clone` 下来改，也可直接 `docker compose up -d` 用现成镜像。上游仅作为代码来源，不建立任何仓库级同步关系。
>
> **配置隔离（硬性约束）**：仓库内**不含任何运行期配置、数据库、密钥、域名或个人数据**。CI 第一步强制扫描全部入库文件，命中凭证或个人配置即构建失败。

---

## 📑 目录

- [一、原项目继承的内容（功能溯源与鸣谢）](#一原项目继承的内容功能溯源与鸣谢)
- [二、本项目新增功能](#二本项目新增功能)
- [三、使用说明（搭建与参数）](#三使用说明搭建与参数)
- [四、许可证与开源协议](#四许可证与开源协议)

---

## 一、原项目继承的内容（功能溯源与鸣谢）

本项目代码源自以下两个上游项目，**完整保留其全部业务能力**，仅在之上做安全加固与独有扩展，未删减任何业务能力。

| 源项目 | 地址 | 说明 |
|--------|------|------|
| 🟢 **项目 1：zeyu8023/emby-pulse**（EmbyPulse 映迹） | [github.com/zeyu8023/emby-pulse](https://github.com/zeyu8023/emby-pulse) | 原始项目，提供全景仪表盘、缺集管理、求片系统、用户中心、数据洞察、映迹工坊等核心能力 |
| 🟡 **项目 2：amlkiller/emby-pulse**（EmbyPulse-next） | [github.com/amlkiller/emby-pulse](https://github.com/amlkiller/emby-pulse) | 本发行版直接继承的代码基线（v1.5.0 起），含 Pro 独占特性与插件系统 |

### 🙏 鸣谢

> 衷心感谢 **[@zeyu8023](https://github.com/zeyu8023)** 与 **[@amlkiller](https://github.com/amlkiller)** 两位作者为本项目提供的上游代码与开源成果。本发行版在保留其全部功能的基础上做安全加固与功能扩展，相关版权声明与仓库链接已在文末许可证章节保留。

### 核心功能（继承自上游，完整保留）

| # | 功能 | 说明 |
|---|------|------|
| 1 | 📊 全景仪表盘 | 实时并发播放监控、IP 归属地、资源概览、播放趋势图表、系统状态 |
| 2 | 🧩 缺集管理 | 比对 Emby 库存与 TMDB 数据，定位缺失集数，联动 MoviePilot 一键搜索 |
| 3 | 📅 追剧日历 | 全球热播剧集更新时间表，红绿灯状态标识已入库 / 缺失 |
| 4 | 🎬 求片系统 | 用户提交电影 / 剧集 / 季度请求，状态链路闭环并通知，机器人协同 |
| 5 | 👤 用户中心 | 账号信息、到期时间、VIP 标识、用户画像、行为追溯 |
| 6 | 🔎 数据洞察 | 用户画像趣味成就、画质审计（4K/HDR/杜比视界占比）、低质筛选 |
| 7 | 🖼️ 映迹工坊 | 多维报表，黑金 / 赛博 / 极光 / 落日等主题，一键生成长图并推送 |
| 8 | 💌 邀请注册 | 生成邀请链接、自助注册、账号到期与批量续期管理 |
| 9 | 🤖 Telegram 机器人 | 播放 / 入库实时推送、风控告警、交互指令、黑名单客户端自动踢出 |
| 10 | 🛠️ 系统运维 | 可视化任务管理、网页端实时日志、数据库工具、系统诊断 |
| 11 | 🕑 播放历史 | 全站播放流水、IP 归属地（IPv4/IPv6）、设备信息展示 |

### Pro 独占特性（继承自上游，完整保留）

- **影巢资源集成**：本地库 + 影巢资源全局搜索、TMDB 联动匹配、一键转存 115 网盘、拼音首字母穿透搜索。
- **风险管控引擎**：并发检测、异常账号违规拦截（警告 / 封禁 / 踢下线）、VIP 豁免、执法记录追溯。
- **积分系统**：每日签到、积分商城（账号续期 / 邀请码等）、求片消费扣费。
- **自定义通知模板**：动漫风 / 幽默风 / 简约风等多套预设，支持变量自由组合与随机文案。
- **智能合集**：TMDB 热门 / 高分电影剧集同步、正在热播追踪、定时自动更新。
- **保号规则**：每月最低播放时长 / 天数要求，不达标自动警告或禁用，VIP 豁免。
- **邮件系统**：注册邮箱验证、密码重置、系统事件邮件通知。
- **媒体去重**：智能扫描重复资源，多维对比分辨率 / 码率 / 编码 / HDR，推荐删除低质量。

### 插件系统（继承自上游，完整保留）

| 插件 | 功能 | 说明 |
|------|------|------|
| 影巢资源 | 资源搜索 + 115 转存 | 全局搜索集成，一键转存 |
| 115 网盘 | 链接自动转存 | 管理员发送链接自动转存 |
| 保号规则 | 播放时长达标检测 | Pro 专享 |
| 通知模板 | 自定义通知风格 | 动漫风、幽默风等 |
| 智能合集 | TMDB 热门合集 | Pro 专享 |

---

## 二、本项目新增功能

> 本节汇总本发行版**相对上游的独有扩展与持续更新**（含 1.9.x 各版本）。

### 2.1 安全加固（首轮，5 项修复）

| # | 问题 | 现象 | 根因 | 改动 |
|---|---|---|---|---|
| 1 | 外网用户管理无法添加用户 | `POST /api/manage/user/new` → 403 | Cloudflare Tunnel 回源下 `X-Forwarded-Proto` 未被 uvicorn 采信，CSRF 期望源算成 http | `Dockerfile` / `docker-compose.yml` 注入 `FORWARDED_ALLOW_IPS` + 应用层兜底 |
| 2 | 外网求片心愿提交失败 | `csfr验证失败：origin不匹配` | 与问题 1 同一根因的两个接口 | 同上 |
| 3 | 明文传送 token | Webhook 地址为 `/api/v1/webhook?token=<明文>` | token 进入访问日志、浏览器历史、`Referer` | `webhook.py` / `bot.py` / `settings.html` 脱敏 |
| 4 | 24 个 API 不鉴权 | 未登录可读统计 / PWA / 壁纸等接口 | 鉴权散落各路由，漏写即放行 | 新增默认拒绝 `AuthGateMiddleware` |
| 5 | F12 泄露一切 | 设置接口回传明文 token，前端把 token 拼进 URL | 后端配置原样吐给浏览器 | `bot.py` / `settings.html` 脱敏 + 模板不再拼 URL |

**双层修复要点**：
- **网关层（主修复）**：默认注入 `FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`，让 uvicorn 采信内网反代下发的 `X-Forwarded-Proto`，`base_url` 恢复为 `https://域名`。
- **应用层（兜底）**：`_expected_origins()` 在检测到 `X-Forwarded-Proto` 时按该协议 + 请求自身 Host 追加允许项，netloc 必须仍与请求 Host 完全一致，**不削弱 CSRF 防护**。
- **默认拒绝 API 网关**：新增 `AuthGateMiddleware`，所有 `/api/**` 默认要求登录、管理员前缀要求管理员会话、401/403 分明，可用 `AUTH_GATE_MODE=observe` 灰度。

部署侧另重写 `docker-compose.yml`（bind mount + healthcheck + 日志轮转）、`Dockerfile`（加固默认值 + 构建自检）、`.github/workflows/docker-publish.yml`（凭证扫描 → 补丁断言 → 语法编译 → 构建 → 容器冒烟 → 推送 GHCR）。

### 2.2 求片 → TgtoDrive 自动同步插件（独有特性）

> 插件 id：`tg_whitelist_sync`（EMBYPULSE 侧，本文档称**插件 A**），作者 Abson2。这是本独立仓库**相对上游的独有扩展**，上游并不具备。配套 TgtoDrive 侧清理插件（**插件 B** `emby-pulse-monitor`）位于仓库 `tgtdrive-monitor/`，需安装到 TgtoDrive 容器内运行。

把工单大厅里用户提交的求片（以 **TMDB 编号**为唯一标识），在管理员**审批通过 / 手动接单**时，**自动写入飞牛上 TgtoDrive 的「光鸭云盘 → 频道监控配置 → 关键词过滤 → 关键词白名单」**，触发 tg 频道监控入库。

- **插件 A（推送 + 删单联动）**：合并写入时**完整保留用户已有的其它关键词**；推送成功后工单置「自动下载中(`status=1`)」；工单被删除时按 `(tmdb_id, season)` **只移除该工单的 `kw_` 规则**。
- **EMBYPULSE 核心（入库流转）**：Emby webhook 触发 `auto_finish_request`，工单自动置「已入库(`status=2`)」并通知。
- **插件 B（TgtoDrive 侧，负责清理）**：周期读取白名单里 `kw_` 规则，以「影视已入库 Emby」判定完结，满 `cleanup_days`（默认 5）天后删除。**只删本插件所有的 `kw_` 规则**。
- **审批拒绝(reject) 不触发推送**，流程直接流转为「已拒绝(`status=3`)」。

通信地址按 `tgtdrive_base_url`(UI) > `TGTODRIVE_URL`(env) > `config/tgtodrive.json` > docker 宿主桥网关(:12366) 优先级解析；**切勿填 NAS 的 LAN IP**。完整参数表、生命周期时序与字段映射见 `docs/tg-whitelist-sync-design.md` 与 `tgtdrive-monitor/README.md`。

### 2.3 系统级 Bark 推送（管理端，独立于 TG / 企业微信）

在「**系统设置 → 系统级 Bark 推送**」卡片填写服务器（默认 `https://api.day.app`，支持自建）与 Device Key，打开总开关，再按需打开「新片入库通知」「新的求片通知」两个独立开关，点「保存 Bark」可用「测试推送」验证。该卡片独立于顶部「保存配置」按钮，只提交 `bark` 节点，不会覆盖 TG / 企业微信渠道配置。

- Bark 是**系统级独立通道**，不加入 `notify_rules` 渠道勾选列表，也不受原有通知总开关约束；推送失败只记日志，绝不影响求片提交 / 入库通知主流程。
- **1.9.2 起**：新片入库 Bark 通知不再依赖 TG Bot / 企业微信配置——早期版本若两者都未配置，`NotificationBot` 不会订阅 `notify.library.new_item` / `notify.library.new_episode`，入库事件被事件总线静默丢弃。升级后事件订阅始终建立。
- **1.9.3 起**：Bark 新片入库正文改走「自定义通知模板」插件（`library_new_item` / `library_new_episode`），与管理端 TG / 企业微信走同一套风格模板，保证多渠道文案一致；并已修复「仅启用 Bark 时新片入库不推送」的遗留问题。

### 2.4 用户端口「通知设置」（1.9.3 新增，需求 5）

面向**用户本人**的推送能力，与 2.3 的**管理端系统级 Bark 互不干预**：用户在自己手机 / 网页端「我的 → 通知设置」里自助配置。

- **两种推送渠道自选**：A. 用户自定义 Bark 推送链接；B. 用户自定义 Webhook 推送。
- **四类推送内容开关**：① 新片入库通知；② 管理员发送的消息；③ 管理员发布的全站公告；④ 用户提交的心愿清单（我的-我的心愿-我的求片）中影视状态变化情况（**有变动才推，无变动不推**）。
- **安全约束**：推送内容**不附带任何网址**（经反代后无法推送最新网址，反而可能暴露真实端口）；任意单用户推送失败均被吞掉，绝不影响入库 / 消息 / 公告 / 心愿主流程。

### 2.5 工单大厅拒绝按钮 + 移动端优化（1.9.3）

- 单条审批流程在「推给 MP / 手动接单」之后、**影巢按钮之前**新增**拒绝**按钮（红标签），复用既有拒绝弹窗逻辑。
- 移动端批量操作悬浮栏左移并增加底部留白，修复「手机端批量勾选后按钮被遮挡」问题；各按钮功能联动保持一致。

### 2.6 关于与支持页链接修正（1.9.3）

- 关于页 GitHub 跳转链接已替换为本项目仓库 `https://github.com/Abson2/emby-pulse`；
- 官方 TG 频道 / TG 交流群组跳转链接置空（展示为「暂未开放」卡片），保留三列布局。

### 2.7 1.9.4 修复与可观测性

- **修复用户端口「通知设置」入口点击无反应**：1.9.3 中通知设置的 state 与方法误定义在 `tab_profile.html` 头像卡的嵌套 `x-data` 作用域内，父级 `requestApp` 组件中的按钮与弹窗无法访问。1.9.4 已将全部 state / 方法迁移至 `request_app.js` 主组件，入口按钮与弹窗恢复正常交互；接口契约与 UI 结构未变。
- **入库通知全链诊断日志**：Webhook 入队、入库事件接收、Bark 推送结果三个节点均输出日志，任一段断裂可从 `docker logs` 直接定位；服务启动增加 per-service 容错，单服务异常不再连锁中断事件订阅。
- **Webhook 测试说明**：Emby Webhook 的「测试」事件返回成功仅代表端点与 token 正常，`test` 类事件不会产生通知（设计如此）；请添加真实影视触发 `item.added` 后观察日志链。

### 2.8 v1.9.5 修复与整合

- **修复用户端「通知设置」点击无反应**：根因是 `request_app.js` 的 URL 查询版本号长期未变（`?v=1.3.53`），浏览器持续使用缓存旧 JS。已升级为 `?v=1.9.5`，**后续每次发版必须同步递增**；升级后如遇无反应，硬刷新（Ctrl/Cmd+Shift+R）即可。
- **推送场景总控新增 Bark 推送**：「通知与机器人」页新增「Bark 推送」配置卡片（启用开关 / 服务器 / Device Key / 新片入库 / 新求片 / 提示音 / 分组 / 保存 / 测试），「通知渠道」勾选组同步新增 Bark 选项；与系统设置页的 Bark 卡片读写同一份配置。
- **Webhook 入口整合**：系统设置页「系统神经感知」改为引导卡；Webhook 的 Token 管理与**带 `?token=` 的完整回调地址**（可直接复制）统一到「通知与机器人 → 推送场景总控 → 新资源入库通知」，保存走 `/api/bot/settings`（脱敏 / 环境变量值不覆盖）。
- **工单大厅移动端批量栏横滑**：底部悬浮批量栏新增 iOS 惯性滚动与鼠标 / 触摸拖拽横滑，按钮多时左右拖动浏览，避免显示不完整。

### 2.9 v1.9.6 通知设置重构与模块收敛

- **用户端「通知设置」彻底重构为独立页面**：1.9.4 / 1.9.5 两轮修复（Alpine 作用域、JS 缓存版本号）后仍有用户反馈点击无反应，1.9.6 按反馈**彻底重构**——新增独立页面 `GET /user/notify_settings`（`templates/user_notify.html`，纯 HTML + 原生 JS，零 Alpine 零缓存问题），「我的 → 通知设置」按钮改为链接直达；接口契约、双渠道（Bark / Webhook 二选一）、四类内容开关（新片入库 / 管理员消息 / 全站公告 / 心愿清单）均不变。未登录访问自动跳转登录页。
- **修复机器人助手保存报 500**：`BotSettingsModel` 补上 `webhook_token` 字段（此前保存触发 `AttributeError`，系统日志被 500 堆栈刷屏）；保存仍复用 `should_update_sensitive_bot_setting`，环境变量控制 / 空值 / 脱敏值均不覆盖。
- **系统设置删除 Webhook 与 Bark 两个模块**：按用户要求整卡移除（含保存 / 测试函数），配置入口统一收敛至「通知与机器人 → 推送场景总控」，避免多入口重复维护。
- **.env 控制 Token 的输入体验**：环境变量控制 Webhook Token 时输入框不再禁用，可粘贴 `.env` 原值仅用于本地生成「带 `?token=` 的完整回调地址」，保存时后端自动忽略、不覆盖 `.env` 配置。

### 2.10 v1.9.7 入库推送修复 / 通知设置回归页面内弹窗

- **新片入库 Bark 推送真正打通（对标企业微信链路）**：全链勘察确认 Emby Webhook → 队列 → 事件总线 → handler 的上游与企业微信完全共用，差异只在 handler 内部分叉；企微由 `channels` 列表驱动，而 Bark 走自身独立开关，前端 1.9.5 写入的 `'bark'` 是后端完全不消费的死值。修复三个真实断点：
  1. 机器人助手页「Bark 推送」卡片有独立「保存 Bark」按钮，**顶部「保存配置」不提交该卡片字段**，且保存成功后 `init()` 会重读并清空表单——用户填完 Device Key 只点顶部保存，配置根本没有落盘。1.9.7 起顶部保存会一并提交 Bark 卡片并等待结果。
  2. `library_notify_channels` 里的 `'bark'` 由 `bark_push.safe_push()` 按事件类型精确折算为启用信号（借鉴企微 channels 驱动），勾了即生效；按 `library_new` / `request_new` 分别判断，避免入库勾选串扰求片、或求片被重复推送。
  3. 未推送原因（未启用 / 缺 Device Key / 服务器地址非法）明确写入系统日志，可自证，不再只有一句静默 INFO。
- **配置边界澄清**：`.env` 中的 `WEBHOOK_TOKEN` 只是 Emby Webhook 回调地址 `?token=` 的**接口校验凭证**，属于「入库事件入口」；Bark 的服务器 / Device Key / 开关属于「推送出口」，只从 `config.json` 读写。两条链路没有配置耦合，Bark 不推送与 `.env` 无关；本版也未引入任何 `BARK_*` 环境变量。
- **用户端通知设置回归页面内弹窗**：1.9.6 独立页打不开的物理层根因是 10308 用户端 wrapper 的路径白名单不含 `/user/notify_settings`，直接 403「非法越界」（与 Alpine 作用域、JS 缓存均无关）。按反馈改为与「媒体库设置」一致的页面内弹窗（纯原生 JS、随 HTML 下发、零 Alpine / teleport），「媒体库设置 / 通知设置 / 娱乐功能」三按钮一排并与上方「我的心愿 / 我的报错 / 追剧更新」对齐；独立页文件已删除，旧缓存 / 收藏链接由服务端 303 回 `/request?tab=profile&notify=1` 并自动打开弹窗，白名单保留放行以免 403。
- **管理端通知设置增加真实 Bark 开关**：「工单大厅通知设置」（工单提交 / 新追新 / 状态变更 / 用户报错）与「用户管理通知设置」（用户注册 / 用户登录 / 用户删除）七类均新增 Bark 渠道勾选；`CHANNEL_OPTIONS` 纳入 `bark`，后端 6 个触发点接入 `safe_rule_push()` 真实推送（求片提交复用既有 `safe_push()` 门控，避免重复推送），不是只保存不消费的假开关。管理端 Telegram HTML 文案在 Bark 侧自动去标签。

### 2.11 v1.9.8 修复 Emby 入库 Webhook 事件名不匹配（Bark 新片入库不推的真正根因）

- **症状**：1.9.7 升级后用户端通知设置已正常、能收到管理员消息，但**收不到新片入库**；管理端 Bark 同样收不到入库推送。这正好说明问题不在 Bark 配置 / 渠道开关 / 用户级推送链路，而在更上游——入库事件根本没进入事件总线（用户级「新片入库」广播与系统级 Bark 共用同一个 `handle_library_new_item`，上游断了两者都不触发，而管理员消息走的是另一条链路所以正常）。
- **根因**：Emby Webhooks 插件发出的是 `ItemAdded` / `LibraryChanged` 这类驼峰事件名，且 `LibraryChanged` 用 `ItemsAdded` **数组**承载新增条目，不是单个 `Item`；而 `webhook.py` 只做 `.lower()`，下游 `notification_bot_webhook_event_service` 只认 `item.added` / `library.new`。事件名与载荷双重不匹配，入库事件被静默丢弃，日志里连 `🔔 触发事件: item.added` 都不会出现。
- **改造（方案 A，治本）**：新增纯函数模块 `app/domains/system/webhook_events.py`，在 `emby_webhook` 入口做三件事：
  1. **事件名归一化**：`EMBY_TO_INTERNAL` 映射表把 `ItemAdded` / `LibraryItemAdded` → `item.added`、`LibraryChanged` → `library.new`、`PlaybackStart/Stop` → `playback.start/stop`、`AuthenticationSuccess` → `login`、`ItemRemoved/Deleted` → `remove`；查表前去掉空格/点/下划线，兼容 `Item Added` 等写法；已是内部格式的事件名保持原样，避免把 `library.new` 打散成匹配不上的 `librarynew`。
  2. **LibraryChanged 拆条**：把 `ItemsAdded` 数组逐条发布为 `item.added`。元素是字符串就补成 `{"Item": {"Id": ...}}`，是对象就保留 `Type` / `SeriesId` / `Name` 等字段（利于剧集分组与季海报插件）。只带 Id 也没关系，下游 `push_single_item` 会调 Emby API `/Items/{Id}` 补全明细，不会推出「未知影片」。
  3. **双事件去重**：Emby 常同时勾选 `Item Added` 与 `Library Changed`，同一部片会来两次；除队列内按 Id 去重外，新增 5 分钟进程内短期去重，避免收到两条 Bark / 两条用户端通知。另外对未识别事件名按名称**限流告警**（每个名字只打一次），方便核对 Emby 实际发了什么，不会被 `SessionStarted` 等高频事件刷屏。
- **回归**：新增 `tests/test_webhook_events.py`（47 项纯标准库断言），含端到端验证「Emby 原生 `ItemAdded` / `LibraryChanged` 载荷 → 入库任务真实入队」以及旧缺陷复现「未归一化的 `itemadded` 不入队」，已接入 CI 第一关。

### 2.12 配置隔离与独立仓库

- 仓库不含任何运行期配置 / 数据库 / 密钥 / 域名 / 个人数据；`.env` / `config/` / `data/` / `*.db` 均被双重忽略。
- 源码通过 GitHub Git Blob API 按 tree 逐 sha 拉取，不接触本机或 NAS 上任何运行期文件；CI 第一步扫描全部入库文件，命中凭证即构建失败。
- 迁移：把原 `config.json` 放到 `config/`、`*.db` 放到 `data/` 即可；切换后所有用户需重新登录。

---

## 三、使用说明（搭建与参数）

### 3.1 快速部署（推荐：直接拉取镜像）

先把环境模板复制出来：

```bash
cp .env.example .env
```

编辑本机 `.env`（已忽略，不进仓库 / 镜像）。**可直接照抄的完整模板**：

```bash
# ===== Emby 连接 =====
# Emby 内网地址：http://<Emby局域网IP>:8096（本机部署填 http://127.0.0.1:8096）
EMBY_HOST=http://<Emby局域网IP>:8096

# Emby 后台「控制台 → API 密钥 → 新建密钥」生成的 Key（用环境变量注入后自动从磁盘配置清除）
EMBY_API_KEY=

# 会话加密密钥（必填！生成：openssl rand -hex 32），留空会导致每次重启所有用户掉登录
SECRET_KEY=

# ===== 按需启用（留空即不启用对应功能）=====
# TG_BOT_TOKEN=           # Telegram 机器人 Token（BotFather 创建）
# TMDB_API_KEY=           # TMDB API Key（缺集扫描 / 合集同步用）
# WEBHOOK_TOKEN=          # Webhook 安全令牌（生成：openssl rand -hex 16）
#   Emby Webhook 插件填：http://<宿主IP>:10307/api/v1/webhook?token=<此值>

# ===== 求片→TgtoDrive 白名单自动同步插件（本项目独有特性）=====
# TgtoDrive 默认 host 网络、端口 12366。从本容器访问它，稳定地址是 docker 宿主桥网关。
# 切勿填 NAS 的 LAN IP（会随 DHCP 变化）。插件另有 UI / tgtodrive.json / 默认网关 多层回落。
TGTODRIVE_URL=http://<DOCKER_HOST_GATEWAY>:12366

# ===== 本版本加固相关（一般无需改动）=====
TZ=Asia/Shanghai
FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
AUTH_GATE_MODE=enforce
WEBHOOK_ALLOW_QUERY_TOKEN=1   # Emby Webhook 插件只能配 ?token= URL；反代可注入 Header 时才改为 0
# PUBLIC_API_PATHS=
```

**各变量说明**：

| 变量 | 是否必填 | 怎么获取 / 说明 |
|------|----------|----------------|
| `EMBY_HOST` | 推荐 | Emby 后台「控制台 → 服务器信息」查看；本机填 `http://127.0.0.1:8096` |
| `EMBY_API_KEY` | 推荐 | Emby 后台「控制台 → API 密钥 → 新建密钥」生成并复制 |
| `SECRET_KEY` | **必填** | `openssl rand -hex 32` 生成的随机串；留空会丢登录 |
| `TG_BOT_TOKEN` / `TMDB_API_KEY` / `WEBHOOK_TOKEN` | 可选 | 按需启用 |
| `TGTODRIVE_URL` | 推荐（用本插件必填） | `http://<DOCKER_HOST_GATEWAY>:12366`；host 网络下填 docker 宿主桥网关，**勿填 LAN IP** |
| `FORWARDED_ALLOW_IPS` 等加固变量 | 一般不动 | 默认已加固 |

### 3.2 `docker-compose.yml`（拉取镜像）

镜像支持**版本锁定**：在 `.env` 里设置 `IMAGE_TAG`（如 `IMAGE_TAG=1.9.8`）拉取指定版本，不设置则默认 `1.9.9`。已发布版本见 [CHANGELOG](CHANGELOG.md) 与 [GitHub Releases](https://github.com/Abson2/emby-pulse/releases)。

```yaml
services:
  embypulse-pro:
    image: ghcr.io/abson2/emby-pulse-hardened:${IMAGE_TAG:-1.9.9}
    container_name: embypulse-pro
    restart: unless-stopped
    ports:
      - "10307:10307"   # 管理端口（建议仅内网暴露）
      - "10308:10308"   # 用户端口（可对外）
    volumes:
      - ./config:/workspace/config
      - ./data:/workspace/data
      # - /path/to/emby/data:/emby-data
    environment:
      - TZ=Asia/Shanghai
      - EMBY_HOST=${EMBY_HOST}
      - "FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    env_file:
      - ./.env
      # ↑ .env 中的 TGTODRIVE_URL 经此注入容器，供「求片→TgtoDrive 自动同步」插件使用；
      #   host 网络下它应指向 docker 宿主桥网关 :12366。
      # - PORT=10307
      # - REQUEST_PORT=10308
      # - DB_PATH=/emby-data/playback_reporting.db
      # - CORS_ORIGINS=https://example.com
      # - FORCE_MIGRATE=1
```

> 只开放注册 / 求片时，**仅暴露 10308** 即可，10307 留在内网更安全。

### 3.3 启动

```bash
docker compose up -d
```

Cloudflare Tunnel 回源指向 `http://<容器宿主机>:10307`（或 10308），tunnel 侧无需额外配置请求头。

### 3.4 Cloudflare Tunnel 内网穿透

本项目要解决的「外网添加用户失败 / 求片 CSRF」正是 Cloudflare Tunnel 回源场景下的典型问题。关于 Cloudflare Tunnel 内网穿透的部署与原理，参见项目介绍：<https://tunnel.naspk.com>。

### 3.5 从源码构建 / 自行修改

直接 `git clone` 后照常 `docker compose up -d` 即可（镜像已含全部加固与独有插件）：

```bash
git clone https://github.com/Abson2/emby-pulse.git
cd emby-pulse
cp .env.example .env
docker compose up -d
```

如需改源码后自己构建镜像，把 `image:` 换成你自己的镜像名，或在 GitHub Actions 里修改推送目标即可（CI 步骤见仓库 `SECURITY.md`）。

### 3.6 配置项总表

| 变量 | 默认值 | 作用 |
|---|---|---|
| `EMBY_HOST` | `http://127.0.0.1:8096` | Emby 服务地址 |
| `EMBY_API_KEY` | 空 | Emby API Key |
| `SECRET_KEY` | 空 | 会话加密密钥，务必随机 |
| `TG_BOT_TOKEN` / `TMDB_API_KEY` / `WEBHOOK_TOKEN` | 空 | 按需启用 |
| `TGTODRIVE_URL` | 无（用独有插件建议设） | TgtoDrive 地址；host 网络下填 `http://<DOCKER_HOST_GATEWAY>:12366` |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` | 允许 uvicorn 采信代理头；**问题 1/2 主修复** |
| `AUTH_GATE_MODE` | `enforce` | `enforce` 拦截 / `observe` 只记录 / `off` 关闭 |
| `PUBLIC_API_PATHS` | 空 | 追加放行路径，逗号分隔 |
| `WEBHOOK_ALLOW_QUERY_TOKEN` | `1` | 是否兼容 `?token=` URL 传参（Emby Webhook 插件必需）；`0` = 仅允许 `X-Webhook-Token` 请求头 |
| `TZ` / `HTTP_PORT` | `Asia/Shanghai` / `10307` | 时区 / 宿主机端口 |

完整说明见 `SECURITY.md`；`.env` 各字段获取方式见 **3.1**。插件 A 自身的运行期配置（TgtoDrive 令牌 / 场景 / 筛选字段 / 轮询兜底间隔 / 触发状态 / 试运行等）在 EMBYPULSE「插件」页内填写，不通过环境变量，详见 **2.2**。

### 3.7 Emby Webhook 回调地址（1.9.0）

在 Emby Webhook 插件里填 `http://<内网IP>:10307/api/v1/webhook?token=<WEBHOOK_TOKEN 的原值>`；带 Token 的完整地址可在「通知与机器人 → 推送场景总控 → 新资源入库通知」一键复制（1.9.6 起原系统设置页入口已移除，统一在此配置）。后端默认已兼容 `?token=`。该回调走**内网直连**，不经过公网链路。

> **1.9.8 起**：Emby Webhooks 插件请勾选 `Item Added` 与 `Library Changed` 两项入库事件，系统会自动把驼峰事件名翻译成内部 `item.added` 并把 `ItemsAdded` 数组拆条；两个都勾也不会重复推送（5 分钟内按 `Item.Id` 去重）。若入库仍收不到通知，先看容器日志有没有 `🔔 触发事件: item.added`；一条都没有说明 Webhook 根本没到达本服务，请核对插件里的回调 URL 与 Token；若出现 `[Webhook] 未识别 Event=`，把该事件名反馈回来补进映射表即可。

> **1.9.9 起**：Emby Webhooks 还会发出 `LibraryDeleted`（整个媒体库被移除）、`system.notificationtest`（后台「发送测试通知」按钮）、`SessionStarted/Stopped`、`UserDataSaved` 等**非入库、无业务动作**的系统/测试事件。这些事件不再打印「未识别 Event=」告警，也不会被发布到事件总线（避免 `library.deleted` 被下游 `delete` 子串误判为「媒体被删除」通知），可放心忽略；只有真正未知且可能是入库类的事件才会继续告警。

### 3.8 用户端口「通知设置」使用说明（1.9.3 新增，1.9.7 回归页面内弹窗）

1. 用户访问 `http://<宿主机>:10308`，登录后进入「**我的**」；
2. 在「媒体库设置 / **通知设置** / 娱乐功能」这一排中点击「**通知设置**」，页面内弹出设置窗口（1.9.7 起不再跳转独立页面，与「媒体库设置」交互一致）：
   - 选择推送渠道：**Bark 链接**（填写 `https://<server>/<device-key>`）或 **Webhook**（填写自定义 POST 地址）；
   - 打开需要的推送内容开关：新片入库 / 管理员消息 / 全站公告 / 心愿清单状态变动；
   - 提示「推送内容不含网址」已内置，无需用户处理；
3. 点「保存设置」即生效，弹窗自动关闭。此后对应事件发生时，系统按用户配置向其自选渠道推送（与管理端系统级 Bark 互不影响）。

> 旧版本收藏 / 缓存的 `/user/notify_settings` 链接会 303 回「我的」页并自动打开该弹窗，不会出现 403。升级后若无反应，请硬刷新（Ctrl/Cmd+Shift+R）清理旧缓存。

> 用户通知设置存储于 `users_meta.user_notify_settings`，由 `app/domains/users/user_notify_service.py` 负责推送，任意单条失败不影响业务主流程。

---

## 四、许可证与开源协议

本项目基于 MIT 许可证开源，但必须遵守以下附加条款：

- **强制开源**：任何基于本项目进行的二次开发、修改或衍生作品，必须保持开源，并使用同样的协议发布。
- **保留版权**：修改后的作品必须在显著位置保留原项目 EmbyPulse（映迹）的版权声明及仓库链接（项目 1：`https://github.com/zeyu8023/emby-pulse`；项目 2：`https://github.com/amlkiller/emby-pulse`）。
- **商业禁令**：严禁将本项目及其修改版进行闭源封装、捆绑销售或任何形式的非法盈利。

<div align="center">
  <sub>Designed & Developed by EmbyPulse Team · 安全加固独立发行版 by Abson2</sub>
</div>
