# 🛡️ emby-pulse 安全加固独立版（基于 EmbyPulse-next）

**直接拉取 `ghcr.io/abson2/emby-pulse-hardened:latest` 即可解决「外网经 Cloudflare Tunnel 无法添加用户」和「求片心愿提交报 CSRF 验证失败：Origin 不匹配」，并同时封堵三处越权与凭证泄露口子。**

> **镜像名说明**：镜像使用带 `-hardened` 后缀的独立包名。早期版本曾用过 `ghcr.io/abson2/emby-pulse`，该包在旧仓库删除后成为孤儿包，新仓库无权写入，其 `latest` 是**已废弃的旧镜像，请勿拉取**。本版本一律以 `ghcr.io/abson2/emby-pulse-hardened:latest` 为准。

这是**你的独立版本**：仓库是干净的独立公开仓库，**不是 fork、没有 upstream remote、不带上游任何提交历史**。内容是在上游原代码基础上做完善的**完整项目源码**——源码、Dockerfile、compose、CI 全部自持，可以直接 `git clone` 下来改、也可以直接 `docker compose up -d` 用现成镜像。上游只作为代码来源，不建立任何仓库级同步关系。

> **配置隔离（硬性约束）**：仓库内**不含任何运行期配置、数据库、密钥、域名或个人数据**。上游源码按文件树逐 sha 拉取，不读取本机或 NAS 上的任何 `config.json` / `.db` / `.env`。CI 第一步强制扫描全部入库文件，命中凭证或个人配置即构建失败。

---

## 📑 目录

- [一、原项目功能介绍（EmbyPulse-next）](#一原项目功能介绍embypulse-next)
- [二、本版本相对原项目做了什么（简明）](#二本版本相对原项目做了什么简明)
- [三、本次做了哪些改进（详细）](#三本次做了哪些改进详细逐条对应你提的问题)
- [四、本项目独有特性：求片 → TgtoDrive 自动同步插件](#四本项目独有特性求片--tgtodrive-自动同步插件本次更新)
- [五、快速部署（推荐：直接拉取镜像）](#五快速部署推荐直接拉取镜像)
- [六、从源码构建 / 自行修改](#六从源码构建--自行修改)
- [七、配置隔离：仓库里有什么、没有什么](#七配置隔离仓库里有什么没有什么)
- [八、仓库结构](#八仓库结构)
- [九、与上游的关系](#九与上游的关系)
- [十、配置项](#十配置项)

---

## 📺 一、原项目功能介绍（EmbyPulse-next）

> 以下内容整理自上游 `amlkiller/emby-pulse`（项目名 EmbyPulse-next，v1.5.0）。本版本**完整保留这些功能**，仅在之上做安全加固，未删减任何业务能力。

### NEXT 版

NEXT 版是 EmbyPulse-pro 的安全分支，解锁 Pro 全部功能；它基于原作者 Pro 版修改而来，修复了已知重大安全问题，并兼容从原作者版本直接升级。

### 核心功能

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

### Pro 独占特性

- **影巢资源集成**：本地库 + 影巢资源全局搜索、TMDB 联动匹配、一键转存 115 网盘、拼音首字母穿透搜索。
- **风险管控引擎**：并发检测、异常账号违规拦截（警告 / 封禁 / 踢下线）、VIP 豁免、执法记录追溯。
- **积分系统**：每日签到、积分商城（账号续期 / 邀请码等）、求片消费扣费。
- **自定义通知模板**：动漫风 / 幽默风 / 简约风等多套预设，支持变量自由组合与随机文案。
- **智能合集**：TMDB 热门 / 高分电影剧集同步、正在热播追踪、定时自动更新。
- **保号规则**：每月最低播放时长 / 天数要求，不达标自动警告或禁用，VIP 豁免。
- **邮件系统**：注册邮箱验证、密码重置、系统事件邮件通知。
- **媒体去重**：智能扫描重复资源，多维对比分辨率 / 码率 / 编码 / HDR，推荐删除低质量。

### 插件系统

| 插件 | 功能 | 说明 |
|------|------|------|
| 影巢资源 | 资源搜索 + 115 转存 | 全局搜索集成，一键转存 |
| 115 网盘 | 链接自动转存 | 管理员发送链接自动转存 |
| 保号规则 | 播放时长达标检测 | Pro 专享 |
| 通知模板 | 自定义通知风格 | 动漫风、幽默风等 |
| 智能合集 | TMDB 热门合集 | Pro 专享 |

---

## 🔍 二、本版本相对原项目做了什么（简明）

| 维度 | 原项目 | 本版本 |
|------|--------|--------|
| 仓库形态 | `amlkiller/emby-pulse` 上游仓库 | **独立公开仓库，非 fork、无 upstream、单干净 commit**，含完整上游源码且全部自持 |
| 外网添加用户（问题 1） | Cloudflare Tunnel 下 `POST /api/manage/user/new` → 403 | 默认注入 `FORWARDED_ALLOW_IPS`，网关层 + 应用层双层修复 |
| 求片提交 CSRF（问题 2） | `csfr验证失败：origin不匹配`（实际为 CSRF 403） | 同一根因，同上修复 |
| 明文 token（问题 3） | webhook 地址为 `/api/v1/webhook?token=<明文>` | 仅读 `X-Webhook-Token` 请求头，URL 退化为纯路径 |
| 24 个 API 不鉴权（问题 4） | 鉴权散落各路由，漏写即放行 | 新增默认拒绝 `AuthGateMiddleware`（白名单集中、401/403 分明） |
| F12 泄露（问题 5） | 设置接口回传明文 token，前端把 token 拼进 URL | 响应脱敏 + 模板不再拼 URL |
| 部署文件 | 含 `/volume1/docker/...` 示例路径与示例内网 IP | 完全重写 compose / Dockerfile / CI，无任何个人配置 |

> 详细根因与改动见 **第三节**；本次新增的「求片 → TgtoDrive 自动同步」能力见 **第四节**；部署方式见 **第五节**。

---

## 🐞 三、本次做了哪些改进（详细，逐条对应你提的问题）

> 本节对应**第一轮加固**（5 项安全修复）。第二轮新增能力见 **第四节**。

| # | 你提出的问题 | 现象 | 根因 | 改动文件 |
|---|---|---|---|---|
| 1 | 外网用户管理无法添加用户 | `POST /api/manage/user/new` → 403 | 见「根因链路」 | `Dockerfile` / `docker-compose.yml` / `app/core/csrf_middleware.py` |
| 2 | 外网求片心愿提交失败 | `csfr验证失败：origin不匹配`（实际为 CSRF） | 同上，同一根因的两个接口 | 同上 |
| 3 | 《明文传送token》 | Webhook 地址为 `/api/v1/webhook?token=<明文>` | token 进入访问日志、浏览器历史、`Referer` | `app/domains/system/webhook.py`、`app/bot/bot.py`、`templates/settings.html` |
| 4 | 《24个API不鉴权》 | 未登录可读统计/PWA/壁纸等接口 | 鉴权散落各路由，漏写即放行 | `app/core/auth_gate_middleware.py`（新增）、`app/bootstrap/middleware.py` |
| 5 | 《F12 能直接看到我的一切》 | 设置接口回传明文 token，前端把 token 拼进 URL | 后端配置原样吐给浏览器 | `app/bot/bot.py`、`templates/settings.html` |

部署侧另外两项重写：

- **`docker-compose.yml` 完全重写**：不再沿用上游的 `/volume1/docker/...` 示例路径与示例内网 IP，改用 bind mount（`./config`、`./data`）、显式 healthcheck、日志轮转、加固变量默认值；
- **`Dockerfile` 重写**：加入 `FORWARDED_ALLOW_IPS` / `AUTH_GATE_MODE` / `WEBHOOK_ALLOW_QUERY_TOKEN` 默认环境与 OCI 元数据，构建过程带自检；
- **`.github/workflows/docker-publish.yml` 重写**：凭证扫描 → 补丁断言 → 语法编译 → 构建 → 容器冒烟 → 推送 GHCR。

### 根因链路（问题 1 / 2，同一个根因）

```
浏览器 https://<你的域名>
   ↓  Cloudflare Tunnel / cloudflared 回源到 http://<NAS-LAN-IP>:10307
   ↓  请求头带 X-Forwarded-Proto: https（但没有 X-Forwarded-Host）
uvicorn ProxyHeadersMiddleware
   ↓  默认 forwarded_allow_ips=127.0.0.1，docker-proxy 来源 IP 不在信任列表
   ↓  于是 scope["scheme"] 仍为 "http"
request.base_url = http://<你的域名>/
   ↓
CSRFMiddleware._expected_origins() 只给出 http://<你的域名>
   ↓
浏览器提交的 Origin = https://<你的域名> → scheme 不一致 → 403
```

所有写操作都过这一层校验，所以添加用户、求片提交两个接口同时失效；只读页面不受影响，症状看起来才像"只有外网、只有提交时"。

修复是**双层**的：

1. **网关层（主修复）**：镜像与 compose 默认注入 `FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`，让 uvicorn 采信内网反代下发的 `X-Forwarded-Proto`，`base_url` 恢复为 `https://域名`。
2. **应用层（兜底）**：`_expected_origins()` 在检测到 `X-Forwarded-Proto` 时，按该协议 + **请求自身 Host** 追加一个允许项。netloc 必须仍与请求 Host 完全一致，攻击者无法伪造受害者浏览器的 Origin，**不削弱 CSRF 防护**。

真实 Cloudflare Tunnel 环境实测结果：

```
[1] 管理端-添加用户 https → 422  CSRF PASSED（已进入业务校验）
[2] 用户端-求片提交 https → 200  CSRF PASSED（业务提示"请先绑定 Emby 账号"）
[3] 对照-伪造 http Origin → 403  CSRF BLOCKED（防护未被削弱）
[4] 内网基准 http       → 422  CSRF PASSED（内网不受影响）
```

### 问题 3：token 不再出现在 URL

- 服务端默认**只**从 `X-Webhook-Token` 请求头读取凭证；
- `webhook_url` 返回值与设置页展示的 URL 都是纯路径 `/api/v1/webhook`；
- 确需兼容只能配 URL 的旧客户端时，显式设 `WEBHOOK_ALLOW_QUERY_TOKEN=1`，默认关闭。

### 问题 4：默认拒绝的 API 网关

上游把鉴权写在各路由函数里，漏写即放行。AST 审计上游全部 482 个路由后，发现 49 个无鉴权信号接口，其中 `/api` 下写接口 6 个、读接口 11 个（统计、PWA 配置/图标、壁纸、认证设置等）。

新增 `AuthGateMiddleware`，插在会话中间件内层：所有 `/api/**` 默认要求登录、管理员前缀要求管理员会话、白名单集中一处、支持 `Authorization: Bearer <API Token>`、401/403 分明。新增路由忘记写鉴权时，结果是 401 而不是数据泄露。可用 `AUTH_GATE_MODE=observe` 灰度。

### 问题 5：F12 不再等于后台管理面板

- `/api/bot/settings` 等配置响应中的敏感字段统一脱敏后返回，无论凭证来自环境变量还是配置文件；
- 设置页模板不再把输入框中的 token 拼进 URL 预览，改为引导使用请求头。

---

## ✨ 四、本项目独有特性：求片 → TgtoDrive 自动同步插件（本次更新）

> 插件 id：`tg_whitelist_sync`（EMBYPULSE 侧，本文档称**插件 A**），版本 `v2.0.0`，作者 Abson2。这是本独立仓库**相对上游的独有扩展**，上游并不具备。配套的 TgtoDrive 侧清理插件（**插件 B** `emby-pulse-monitor`）位于仓库 `tgtdrive-monitor/`，需安装到 TgtoDrive 容器内运行。

### 4.1 它解决什么问题

把 **EMBYPULSE 工单大厅**里用户提交的求片（以 **TMDB 编号**为唯一标识），在管理员**审批通过 / 手动接单**时，**自动写入飞牛上 TgtoDrive 的「光鸭云盘 → 频道监控配置 → 关键词过滤 → 关键词白名单」**，触发 tg 频道监控入库。

核心分工（避免早期版本「全量覆盖写误删用户关键词」的坑）：

- **插件 A（本插件，只写不删）**：把求片推送到白名单（分辨率 4k / 画面效果 全部效果 / 年份 读 TMDB 自动填 / 必包含关键词 265），合并写入时**完整保留用户已有的其它关键词**；推送成功后把工单置为「自动下载中(`status=1`)」。
- **EMBYPULSE 核心（入库流转）**：Emby webhook 触发 `auto_finish_request`，把工单自动置为「已入库(`status=2`)」并通知管理员/用户。这部分由核心负责，插件 A 不重复实现。
- **插件 B（TgtoDrive 侧，负责清理）**：按计划周期读取白名单里本插件写入的 `kw_` 规则，以「影视已入库 Emby」判定完结，满 `cleanup_days`（默认 5）天后删除对应关键词。**只删 `kw_` 本插件所有的规则，绝不碰用户其它关键词。**

### 4.2 两大组件一览

| 组件 | 运行位置 | 职责 | 默认 |
|------|----------|------|------|
| 🔁 **插件 A：求片推送（tg_whitelist_sync）** | EMBYPULSE 侧 | 审批通过/手动接单 → 写 TgtoDrive 白名单（合并、不删）；推送成功置 `status=1` | 启用即生效；**事件驱动**（工单大厅接单即推送）+ 轮询兜底（`check_interval` 默认 5 分钟） |
| 🧹 **插件 B：完结清理（emby-pulse-monitor）** | TgtoDrive 容器内 | 读白名单 `kw_` 规则 → 按影视是否已入库 Emby 判完结 → 满 `cleanup_days`(默认5) 删对应关键词 | 需安装到 TgtoDrive 并按周期启用；只删本插件所有的 `kw_` 规则 |

### 4.3 生命周期与时序

```
用户提交求片 ──► 管理员「手动接单 / 审批通过」（事件驱动，点完立即推送）
   │  approve → status=1（自动下载中）；manual → status=4（手动接单中）
   ▼
插件 A 立即把该求片推送到 TgtoDrive 白名单（合并写入，绝不删用户规则）
   │  推送成功 → manual 的 4 也流转为 1（自动下载中）
   ▼
TgtoDrive 频道监控命中 → tg 转存 → Emby 入库存档
   ▼
EMBYPULSE 核心（Emby webhook）自动把工单置 status=2（已入库）并通知管理员/用户
   ▼
插件 B（TgtoDrive 侧，周期运行）发现该影视已入库 Emby
   │  开始计时；满 cleanup_days（默认 5 天）
   ▼
删除 TgtoDrive 白名单里 kw_<tmdb>_<season> 关键词（只删本插件的，不动用户其它规则）
```

> 审批**拒绝(reject) 不触发推送**，流程直接流转为「已拒绝(`status=3`)」并通知管理员/用户。
> 事件驱动为主路径，后台轮询仅为容错兜底；单轮异常只记日志、不终止线程。

### 4.4 插件配置项（在 EMBYPULSE「插件」页该插件下填写）

| 配置键 | 默认 | 作用 |
|--------|------|------|
| `tgtdrive_base_url` | 留空 | TgtoDrive 地址；**留空时按优先级自动取**（见 4.5），一般无需填 |
| `tgtdrive_token` | 空 | OpenAPI Bearer 令牌（系统工具 → 实用工具 → OpenAPI 创建） |
| `tgtdrive_cookie` | 空 | 兜底会话 Cookie，仅在白名单接口只认网页会话时填 |
| `tgtdrive_scene` | `ENV_FILTER_GUANGYA` | 白名单场景；光鸭=`ENV_FILTER_GUANGYA`，115=`ENV_FILTER_115`，天翼=`ENV_FILTER_189`，HDHIVE=`ENV_FILTER_HDHIVE` |
| `whitelist_endpoint` | `/api/visual-filter/config` | TgtoDrive 8.8.x 关键词过滤写入路径 |
| `rule_resolution` | `4k` | 写入规则的分辨率筛选 |
| `rule_effect` | `all` | 画面效果筛选，`all`=全部效果 |
| `rule_required_keyword` | `265` | 规则必包含关键词（如 265=H.265/HEVC），留空不限制 |
| `check_interval` | `5` | 后台轮询兜底间隔（分钟） |
| `trigger_mode` | `approved_and_manual` | 仅影响后台轮询兜底：仅审批通过(`1`) / 审批通过+手动接单(`1,4`) / 全部待同步(`1,4,7`)；**事件驱动（工单大厅接单）不受此限** |
| `dry_run` | 关 | 试运行：只记录动作、不写入不改状态，首次验证推荐开启 |
| `notify_enabled` | 关 | 运行结果发全局通知 |

> **职责边界**：入库流转（待入库 `7` → 已入库 `2` + 通知）由 **EMBYPULSE 核心**负责；完结关键词清理由 **TgtoDrive 侧插件 B**（`tgtdrive-monitor/`，见该目录 README）负责。插件 A 自 v2.0.0 起**只推送、不删除**，彻底避免早期「全量覆盖写误删用户关键词」的坑。

### 4.5 通信地址健壮性：不把易变 IP 写死

TgtoDrive 通常以 `host` 网络模式运行（默认端口 `12366`），**不在 EMBYPULSE 所在的 docker 桥网络里**，因此无法用 compose 服务名做 DNS 解析。从 EMBYPULSE 容器看过去，访问 host 网络下 TgtoDrive 的**稳定地址是 docker 宿主桥网关**（即 docker0 网桥地址，飞牛默认通常位于 `172.17.0.0/16` 网段）。

> ⚠️ **不要填 NAS 的 LAN IP**（如 `192.168.x.x`）：它会因 DHCP / 换网而变，容器一重启就可能失联。

插件按以下优先级解析 TgtoDrive 地址（`_resolve_base_url`），**任意一层配置即可恢复通信，容器变动也不用手改代码**：

```
1. 插件配置 UI 填的 tgtdrive_base_url   （最高优先，便于临时覆盖）
2. 环境变量 TGTODRIVE_URL               （在 compose / .env 一处配置，容器重建不丢）← 推荐
3. 本地配置文件 <config>/tgtodrive.json 的 base_url
4. 默认值：docker 宿主桥网关 :12366      （host 网络 TgtoDrive 的稳定入口）
```

配套写入策略已规避 TgtoDrive 的四处坑：① 全量覆盖 —— 先 `GET` 当前场景规则再合并整体 `POST`；② GET 返回是信封（`rules` 在 `current` / `scenes[场景]` 内，顶层没有），按目标场景定位，避免串场景；③ 后端信 `rules` 不信 `raw_regex` —— 只维护 `rules` 数组；④ 仅动本插件写入的 `kw_` 前缀规则，**绝不波及你在页面里手动配置的其他关键词**。

> 若现场发现 POST 后规则不生效或返回 400，可在插件配置「写入体形态」把 `flat` 切到 `envelope`（原样回写整个 GET 信封）。

### 4.6 设计文档

更完整的技术细节（接口结构、字段名映射 `RULE_FIELD`、鉴权兜底、回归测试）见仓库 `docs/tg-whitelist-sync-design.md`。字段名已按 TgtoDrive 真实响应样本（2026-11）定稿：`resolution / effect / year / required_keyword`，并配套 `include_year` / `include_required_keyword` 布尔开关；`year`、`tmdb_id` 为字符串。旧版本推断的 `picture_effect` / `must_include` 后端并不识别，会被丢弃（这正是"必包含关键词丢失"的原因）。字段名仍可通过插件配置「必含字段名」覆盖，无需改代码。

### 4.7 插件 B（TgtoDrive 侧完结清理）怎么启用

> ⚠️ **插件 B 与 EMBYPULSE 插件页里的「求片同步 TgtoDrive 白名单」是两个不同框架的插件，不要互相导入**：前者是 TgtoDrive 单文件插件（`tgtdrive-monitor/plugin.py`，用 `PLUGIN_MANIFEST` + `run`），后者是 EMBYPULSE 插件（`app/plugins/tg_whitelist_sync/plugin.py`，用 `PluginBase`）。把后者当 `sync.py` 塞进 TgtoDrive 会因缺少 `PLUGIN_MANIFEST` 直接报错。

插件 B **不包含在 EMBYPULSE 镜像里**，需单独装到 **TgtoDrive 容器**内，由 TgtoDrive 的 OpenAPI 按计划周期调用。完整步骤、参数表与运行逻辑见 `tgtdrive-monitor/README.md`，要点：

1. 把 `tgtdrive-monitor/plugin.py` 放进 TgtoDrive 插件目录（按 TgtoDrive 的插件命名要求放置，例如 `emby-pulse-monitor.py` 或对应插件 id 的目录）；
2. 在 TgtoDrive 插件管理中启用，填写 `tgtdrive_base_url`（容器内通常用回环 `http://127.0.0.1:12366`）、`tgtdrive_token`、`emby_url` / `emby_key`（用于判定入库），并设置运行周期（建议每天一次或每 6 小时一次）；
3. 纯标准库、无需额外依赖；**只删本插件写入的 `kw_` 规则，绝不碰你在页面里手动配置的其他关键词**；`cleanup_days` 设为 `0` 可关闭清理。

> 地址约定：本目录不在凭证扫描豁免名单内，故插件 B 一律只用回环地址 `127.0.0.1`，不出现任何内网 IP 或第三方域名；若 TgtoDrive / Emby 不在回环上，请在 TgtoDrive 插件配置里改成实际可达地址。

---

## 🚀 五、快速部署（推荐：直接拉取镜像）

### 5.1 准备 `.env`（含 TgtoDrive 地址，小白向）

先把模板复制出来：

```bash
cp .env.example .env
```

然后编辑本机 `.env`（它已被 `.gitignore` / `.dockerignore` 排除，不会进仓库 / 镜像）。下面是**可直接照抄的完整模板**（已包含本轮新增的 `TGTODRIVE_URL`）：

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
# WEBHOOK_TOKEN=          # Webhook 安全令牌（生成：openssl rand -hex 16），配 X-Webhook-Token 请求头

# ===== 求片→TgtoDrive 白名单自动同步插件（本项目独有特性）=====
# TgtoDrive 默认 host 网络、端口 12366。从 EMBYPULSE 容器访问它，稳定地址是 docker 宿主桥网关。
# 绝大多数飞牛 docker 默认桥网关即下方占位值，无需改动；改过 docker 子网请填实际 docker0 网关。
# 切勿填 NAS 的 LAN IP（会随 DHCP 变化）。插件另有 UI / tgtodrive.json / 默认网关 多层回落。
TGTODRIVE_URL=http://<DOCKER_HOST_GATEWAY>:12366

# ===== 本版本加固相关（一般无需改动）=====
TZ=Asia/Shanghai
FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
AUTH_GATE_MODE=enforce
WEBHOOK_ALLOW_QUERY_TOKEN=0
# PUBLIC_API_PATHS=
```

**各变量怎么填**（仅本机编辑，不进仓库）：

| 变量 | 是否必填 | 怎么获取 / 说明 |
|------|----------|----------------|
| `EMBY_HOST` | 推荐 | Emby 后台「控制台 → 服务器信息」查看；本机部署填 `http://127.0.0.1:8096` |
| `EMBY_API_KEY` | 推荐 | Emby 后台「控制台 → API 密钥 → 新建密钥」生成并复制 |
| `SECRET_KEY` | **必填** | `openssl rand -hex 32` 生成的随机串；留空会丢登录 |
| `TG_BOT_TOKEN` / `TMDB_API_KEY` / `WEBHOOK_TOKEN` | 可选 | 按需启用，见上方注释 |
| `TGTODRIVE_URL` | 推荐（用本插件必填） | `http://<DOCKER_HOST_GATEWAY>:12366`；host 网络下填 docker 宿主桥网关，**勿填 LAN IP** |
| `FORWARDED_ALLOW_IPS` 等加固变量 | 一般不动 | 默认已加固，详见 `.env.example` 注释 |

> 其余加固变量（`FORWARDED_ALLOW_IPS` / `AUTH_GATE_MODE` / `WEBHOOK_ALLOW_QUERY_TOKEN` / `PUBLIC_API_PATHS`）一般无需改动，默认值即已加固。

### 5.2 `docker-compose.yml`（拉取镜像，格式参考）

把下面内容保存为 `docker-compose.yml`（仓库根目录已提供同款）。镜像支持**版本锁定**：在 `.env` 里设置 `IMAGE_TAG` 即可拉取指定版本（如 `IMAGE_TAG=1.6.0` 回退历史版本，`IMAGE_TAG=latest` 追最新），不设置则默认 `1.7.0`。已发布版本见 [CHANGELOG](CHANGELOG.md) 与 [GitHub Releases](https://github.com/Abson2/emby-pulse/releases)。

```yaml
services:
  embypulse-pro:
    image: ghcr.io/abson2/emby-pulse-hardened:${IMAGE_TAG:-1.7.0}
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
      # Cloudflare Tunnel 回源经 docker-proxy，uvicorn 默认只信任 127.0.0.1，
      # 会忽略 X-Forwarded-Proto: https，使 CSRF 把期望源算成 http://域名，
      # 与浏览器 https://域名 不匹配。信任内网代理来源以修正 scheme。
      - "FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    env_file:
      - ./.env
      # ↑ .env 中的 TGTODRIVE_URL 经此注入容器，供「求片→TgtoDrive 自动同步」插件使用；
      #   host 网络下它应指向 docker 宿主桥网关 :12366，详见 README 第五节 5.1。
      # - PORT=10307
      # - REQUEST_PORT=10308
      # - DB_PATH=/emby-data/playback_reporting.db
      # - CORS_ORIGINS=https://example.com
      # - FORCE_MIGRATE=1
```

> 只开放注册 / 求片时，**仅暴露 10308** 即可，10307 留在内网更安全。

### 5.3 启动

```bash
docker compose up -d
```

Cloudflare Tunnel 回源指向 `http://<容器宿主机>:10307`（或 10308），tunnel 侧无需额外配置请求头。

### 5.4 Cloudflare Tunnel 内网穿透

本项目要解决的「外网添加用户失败 / 求片 CSRF」正是 Cloudflare Tunnel 回源场景下的典型问题。关于 Cloudflare Tunnel 内网穿透的部署与原理，参见项目介绍：<https://tunnel.naspk.com>。

---

## 🧩 六、从源码构建 / 自行修改

直接 `git clone` 后照常 `docker compose up -d` 即可（镜像已含全部加固与独有插件）：

```bash
git clone https://github.com/Abson2/emby-pulse.git
cd emby-pulse
cp .env.example .env
docker compose up -d
```

如需改源码后自己构建镜像，把 `image:` 换成你自己的镜像名，或在 GitHub Actions 里修改推送目标即可（CI 步骤见仓库 `SECURITY.md`）。

---

## 🔐 七、配置隔离：仓库里有什么、没有什么

| | 本仓库 | 部署时（你的机器） |
|---|---|---|
| 应用源码 | **含**，完整、可改 | 镜像内 / clone 后本地 |
| `config/config.json` | **不含** | `./config` 卷挂载目录内 |
| `data/*.db` | **不含** | `./data` 卷挂载目录内 |
| `.env` / API Key / Token / 域名 | **不含**，`.env.example` 只有中性占位符 | 本机 `.env`，已被忽略 |
| 个人域名、内网 IP、账号 | **不含**（上游示例 IP 已替换为本地回环 / 占位符） | 仅在你的 tunnel / `.env` 中 |
| 上游 git 历史 / fork 关系 | **无**，干净单 commit | — |

配套机制：

- 源码通过 GitHub Git Blob API 按 tree 逐 sha 拉取，**不接触本机或 NAS 上任何运行期文件**；
- 生成时对上游目录做清理：移除上游 CI、内部工具元数据、示例配置与任何数据库/备份文件；
- `.gitignore` + `.dockerignore` 双重排除 `config/`、`data/`、`*.db`、`*.bak*`、`.env` 与密钥文件；
- `tools/check-no-secrets.py` 在 CI 第一步扫描全部入库文件：凭证赋值、Token 前缀、私钥头、SQLite 文件头、配置数据文件、具体内网地址、非示例域名；
- Dockerfile 只 `COPY` 明确列出的运行时目录，不 `COPY . .`；
- 推送完成后，再从 GitHub API 拉回整棵 tree 与全部 blob 复核一遍。

### 从现有部署迁移

本 compose 默认使用 bind mount `./config` 与 `./data`，与上游示例的 `/volume1/docker/...` 路径解耦。要带走现有配置/数据，把原 `config.json` 放到仓库目录下的 `config/`、`*.db` 放到 `data/` 即可；切换后所有用户需重新登录（重建容器会清理在线会话）。

> 切换前先备份原 `data`。

---

## 🗂️ 八、仓库结构

```
.
├── README.md                      本文件：功能介绍 / 改动 / 部署
├── IMPROVEMENTS.md                逐项技术细节与验证方法
├── SECURITY.md                    安全策略、配置、风险边界
├── Dockerfile                     重写：加固默认值 + 构建自检
├── docker-compose.yml             重写：bind mount / healthcheck / 日志轮转
├── .env.example / .gitignore / .dockerignore
├── tools/check-no-secrets.py      CI 凭证扫描
├── .github/workflows/docker-publish.yml   扫描 → 断言 → 构建 → 冒烟 → 推送
├── app/                           完整应用源码
│   ├── core/csrf_middleware.py            ★ 问题 1/2 应用层兜底
│   ├── core/auth_gate_middleware.py      ★ 新增，问题 4
│   ├── bootstrap/middleware.py           ★ 注册鉴权网关
│   ├── domains/system/webhook.py         ★ 问题 3
│   ├── bot/bot.py                        ★ 问题 3/5
│   └── plugins/tg_whitelist_sync/plugin.py   ★ 独有特性：求片→TgtoDrive 同步（插件 A，v2.0.0）
├── tgtdrive-monitor/plugin.py               ★ 独有特性：TgtoDrive 侧完结清理（插件 B）
├── tgtdrive-monitor/README.md               ★ 插件 B 安装与参数说明
├── templates/settings.html        ★ 问题 3/5，完整模板
├── tests/test_tg_whitelist_sync.py    独有特性插件 A 单元测试
├── tests/test_tgtdrive_monitor.py    独有特性插件 B 单元测试（mock urllib）
├── docs/tg-whitelist-sync-design.md    独有特性设计文档
└── static/ run.py pyproject.toml uv.lock requirements.txt ...
```

★ = 本仓库相对上游的改动/新增。其中前 7 个源码文件 + `templates/settings.html` 为第一轮安全加固；`app/plugins/tg_whitelist_sync/plugin.py`（插件 A）+ `tgtdrive-monitor/`（插件 B）+ 测试 + 设计文档为第二轮独有特性扩展。其余源码保持上游原样，便于对照与后续自行升级。

---

## 🔗 九、与上游的关系

- 上游：`https://github.com/amlkiller/emby-pulse`（**仅作为代码来源**）；
- 本仓库**不是 fork**：无 upstream remote、无上游提交历史、单个干净 root commit；
- 不会自动同步上游更新；升级需人工对比后提交到本仓库；
- 上游仓库未附带 LICENSE，请自行评估再分发合规性，详见 `SECURITY.md`。

---

## ⚙️ 十、配置项

| 变量 | 默认值 | 作用 |
|---|---|---|
| `EMBY_HOST` | `http://127.0.0.1:8096` | Emby 服务地址（见 5.1 获取方式） |
| `EMBY_API_KEY` | 空 | Emby API Key（见 5.1） |
| `SECRET_KEY` | 空 | 会话加密密钥，务必随机（见 5.1） |
| `TG_BOT_TOKEN` / `TMDB_API_KEY` / `WEBHOOK_TOKEN` | 空 | 按需启用（见 5.1） |
| `TGTODRIVE_URL` | 无（用独有插件建议设） | TgtoDrive 地址；host 网络下填 `http://<DOCKER_HOST_GATEWAY>:12366`。插件按 UI > 本变量 > `config/tgtodrive.json` > 默认网关 优先级解析（见第四节 4.5） |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` | 允许 uvicorn 采信代理头；**问题 1/2 主修复** |
| `AUTH_GATE_MODE` | `enforce` | `enforce` 拦截 / `observe` 只记录 / `off` 关闭 |
| `PUBLIC_API_PATHS` | 空 | 追加放行路径，逗号分隔 |
| `WEBHOOK_ALLOW_QUERY_TOKEN` | `0` | 是否兼容旧的 `?token=` 传参 |
| `TZ` / `HTTP_PORT` | `Asia/Shanghai` / `10307` | 时区 / 宿主机端口 |

完整说明见 `SECURITY.md`；`.env` 各字段获取方式见 **第五节 5.1**。插件 A 自身的运行期配置（TgtoDrive 令牌 / 场景 / 筛选字段 / 轮询兜底间隔 / 触发状态 / 试运行等）在 EMBYPULSE「插件」页内填写，不通过环境变量，详见 **第四节 4.4**。配套清理插件 B 的安装与参数见 `tgtdrive-monitor/README.md`（需装到 TgtoDrive 容器内运行）。
