# emby-pulse 安全加固独立版（基于 EmbyPulse-next）

**直接拉取 `ghcr.io/abson2/emby-pulse-hardened:latest` 即可解决「外网经 Cloudflare Tunnel 无法添加用户」和「求片心愿提交报 CSRF 验证失败：Origin 不匹配」，并同时封堵三处越权与凭证泄露口子。**

> **镜像名说明**：镜像使用带 `-hardened` 后缀的独立包名。早期版本曾用过 `ghcr.io/abson2/emby-pulse`，该包在旧仓库删除后成为孤儿包，新仓库无权写入，其 `latest` 是**已废弃的旧镜像，请勿拉取**。本版本一律以 `ghcr.io/abson2/emby-pulse-hardened:latest` 为准。

这是**你的独立版本**：仓库是干净的独立公开仓库，**不是 fork、没有 upstream remote、不带上游任何提交历史**。内容是在上游原代码基础上做完善的**完整项目源码**——源码、Dockerfile、compose、CI 全部自持，可以直接 `git clone` 下来改、也可以直接 `docker compose up -d` 用现成镜像。上游只作为代码来源，不建立任何仓库级同步关系。

> **配置隔离（硬性约束）**：仓库内**不含任何运行期配置、数据库、密钥、域名或个人数据**。上游源码按文件树逐 sha 拉取，不读取本机或 NAS 上的任何 `config.json` / `.db` / `.env`。CI 第一步强制扫描全部入库文件，命中凭证或个人配置即构建失败。

---

## 一、原项目功能介绍（EmbyPulse-next）

> 以下内容整理自上游 `amlkiller/emby-pulse`（项目名 EmbyPulse-next，v1.5.0）。本版本**完整保留这些功能**，仅在之上做安全加固，未删减任何业务能力。

### NEXT 版

NEXT 版是 EmbyPulse-pro 的安全分支，解锁 Pro 全部功能；它基于原作者 Pro 版修改而来，修复了已知重大安全问题，并兼容从原作者版本直接升级。

### 核心功能

1. **全景仪表盘**：实时并发播放监控、IP 归属地、资源概览、播放趋势图表、系统状态。
2. **缺集管理**：比对 Emby 库存与 TMDB 数据，定位缺失集数，联动 MoviePilot 一键搜索、智能匹配版本。
3. **追剧日历**：全球热播剧集更新时间表，红绿灯状态标识已入库 / 缺失，一键跳转播放或复制搜索指令。
4. **求片系统**：用户提交电影 / 剧集 / 季度请求，状态链路完整闭环（待处理 → 已下载 → 已入库 / 已拒绝）并通知用户，机器人协同。
5. **用户中心**：账号信息、到期时间、VIP 标识、用户画像、行为追溯。
6. **数据洞察**：用户画像趣味成就、入驻溯源、画质审计（4K/HDR/杜比视界占比）、低质筛选。
7. **映迹工坊**：多维报表（日 / 周 / 月 / 年），黑金 / 赛博 / 极光 / 落日等主题，一键生成长图并推送。
8. **邀请注册**：生成邀请链接、自助注册、账号到期与批量续期管理。
9. **Telegram 机器人**：播放 / 入库实时推送、风控告警、交互指令（`/stats`、`/search`、`/check`）、黑名单客户端自动踢出。
10. **系统运维**：可视化任务管理、网页端实时日志、数据库工具（完整性检测 / 修复 / 备份恢复）、系统诊断。
11. **播放历史**：全站播放流水、IP 归属地（IPv4/IPv6）精确查询、设备信息展示。

### Pro 独占特性

- **影巢资源集成**：本地库 + 影巢资源全局搜索、TMDB 联动匹配海报 / 背景 / 简介、一键转存到 115 网盘、拼音首字母穿透搜索。
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

## 二、本版本相对原项目做了什么（简明）

| 维度 | 原项目 | 本版本 |
|------|--------|--------|
| 仓库形态 | `amlkiller/emby-pulse` 上游仓库 | **独立公开仓库，非 fork、无 upstream、单干净 commit**，含完整上游源码且全部自持 |
| 外网添加用户（问题 1） | Cloudflare Tunnel 下 `POST /api/manage/user/new` → 403 | 默认注入 `FORWARDED_ALLOW_IPS`，网关层 + 应用层双层修复 |
| 求片提交 CSRF（问题 2） | `csfr验证失败：origin不匹配`（实际为 CSRF 403） | 同一根因，同上修复 |
| 明文 token（问题 3） | webhook 地址为 `/api/v1/webhook?token=<明文>` | 仅读 `X-Webhook-Token` 请求头，URL 退化为纯路径 |
| 24 个 API 不鉴权（问题 4） | 鉴权散落各路由，漏写即放行 | 新增默认拒绝 `AuthGateMiddleware`（白名单集中、401/403 分明） |
| F12 泄露（问题 5） | 设置接口回传明文 token，前端把 token 拼进 URL | 响应脱敏 + 模板不再拼 URL |
| 部署文件 | 含 `/volume1/docker/...` 示例路径与示例内网 IP | 完全重写 compose / Dockerfile / CI，无任何个人配置 |

详细根因与改动见 **第三节**；部署方式见 **第四节**。

---

## 三、本次做了哪些改进（详细，逐条对应你提的问题）

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

## 四、快速部署（推荐：直接拉取镜像）

### 4.1 准备 .env

```bash
cp .env.example .env
```

按下方说明填写（只在本机编辑，`.env` 已被 `.gitignore` / `.dockerignore` 排除，不会进仓库 / 镜像）：

| 变量 | 是否必填 | 怎么获取 |
|------|----------|----------|
| `EMBY_HOST` | 推荐 | Emby 服务器内网地址，格式 `http://<IP>:8096`，在 Emby 后台「控制台 → 服务器信息」查看；本机部署填 `http://127.0.0.1:8096` |
| `EMBY_API_KEY` | 推荐 | Emby 后台「控制台 → API 密钥 → 新建密钥」生成并复制；用环境变量注入后会从磁盘配置自动清除 |
| `SECRET_KEY` | 必填 | 会话加密密钥，**务必随机**。生成：`openssl rand -hex 32`，把输出填入；留空会导致每次重启所有用户掉登录 |
| `TG_BOT_TOKEN` | 可选 | 在 BotFather 创建机器人得到的 token；启用 Telegram 推送 / 求片才需要 |
| `TMDB_API_KEY` | 可选 | 在 TMDB 官网注册申请的 API Key；用于缺集扫描、合集同步 |
| `WEBHOOK_TOKEN` | 可选（建议） | 自定义随机串，用于 Emby Webhook 回调校验。生成：`openssl rand -hex 16`；配合 `X-Webhook-Token` 请求头使用 |

其余加固变量（`FORWARDED_ALLOW_IPS` / `AUTH_GATE_MODE` / `WEBHOOK_ALLOW_QUERY_TOKEN` / `PUBLIC_API_PATHS`）一般无需改动，默认值即已加固，见 `.env.example` 注释。

### 4.2 docker-compose.yml（拉取镜像，格式参考）

把下面内容保存为 `docker-compose.yml`（仓库根目录已提供同款）：

```yaml
services:
  embypulse-pro:
    image: ghcr.io/abson2/emby-pulse-hardened:latest
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
      # - PORT=10307
      # - REQUEST_PORT=10308
      # - DB_PATH=/emby-data/playback_reporting.db
      # - CORS_ORIGINS=https://example.com
      # - FORCE_MIGRATE=1
```

> 只开放注册 / 求片时，**仅暴露 10308** 即可，10307 留在内网更安全。

### 4.3 启动

```bash
docker compose up -d
```

Cloudflare Tunnel 回源指向 `http://<容器宿主机>:10307`（或 10308），tunnel 侧无需额外配置请求头。

### 4.4 Cloudflare Tunnel 内网穿透

本项目要解决的「外网添加用户失败 / 求片 CSRF」正是 Cloudflare Tunnel 回源场景下的典型问题。关于 Cloudflare Tunnel 内网穿透的部署与原理，参见项目介绍：<https://tunnel.naspk.com>。

---

## 五、从源码构建 / 自行修改

直接 `git clone` 后照常 `docker compose up -d` 即可（镜像已含全部加固）：

```bash
git clone https://github.com/Abson2/emby-pulse.git
cd emby-pulse
cp .env.example .env
docker compose up -d
```

如需改源码后自己构建镜像，把 `image:` 换成你自己的镜像名，或在 GitHub Actions 里修改推送目标即可（CI 步骤见仓库 `SECURITY.md`）。

---

## 六、配置隔离：仓库里有什么、没有什么

| | 本仓库 | 部署时（你的机器） |
|---|---|---|
| 应用源码 | **含**，完整、可改 | 镜像内 / clone 后本地 |
| `config/config.json` | **不含** | `./config` 卷挂载目录内 |
| `data/*.db` | **不含** | `./data` 卷挂载目录内 |
| `.env` / API Key / Token / 域名 | **不含**，`.env.example` 只有中性占位符 | 本机 `.env`，已被忽略 |
| 个人域名、内网 IP、账号 | **不含**（上游示例 IP 已替换为本地回环） | 仅在你的 tunnel / `.env` 中 |
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

## 七、仓库结构

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
├── app/                           完整应用源码（本次改动 7 个文件）
│   ├── core/csrf_middleware.py            ★ 问题 1/2 应用层兜底
│   ├── core/auth_gate_middleware.py      ★ 新增，问题 4
│   ├── bootstrap/middleware.py           ★ 注册鉴权网关
│   ├── domains/system/webhook.py         ★ 问题 3
│   └── bot/bot.py                        ★ 问题 3/5
├── templates/settings.html        ★ 问题 3/5，完整模板
├── static/ run.py pyproject.toml uv.lock requirements.txt ...
```

★ = 本次修改/新增，共 7 个源码文件；其余源码保持上游原样，便于对照与后续自行升级。

---

## 八、与上游的关系

- 上游：`https://github.com/amlkiller/emby-pulse`（**仅作为代码来源**）；
- 本仓库**不是 fork**：无 upstream remote、无上游提交历史、单个干净 root commit；
- 不会自动同步上游更新；升级需人工对比后提交到本仓库；
- 上游仓库未附带 LICENSE，请自行评估再分发合规性，详见 `SECURITY.md`。

---

## 九、配置项

| 变量 | 默认值 | 作用 |
|---|---|---|
| `EMBY_HOST` | `http://127.0.0.1:8096` | Emby 服务地址（见 4.1 获取方式） |
| `EMBY_API_KEY` | 空 | Emby API Key（见 4.1） |
| `SECRET_KEY` | 空 | 会话加密密钥，务必随机（见 4.1） |
| `TG_BOT_TOKEN` / `TMDB_API_KEY` / `WEBHOOK_TOKEN` | 空 | 按需启用（见 4.1） |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` | 允许 uvicorn 采信代理头；**问题 1/2 主修复** |
| `AUTH_GATE_MODE` | `enforce` | `enforce` 拦截 / `observe` 只记录 / `off` 关闭 |
| `PUBLIC_API_PATHS` | 空 | 追加放行路径，逗号分隔 |
| `WEBHOOK_ALLOW_QUERY_TOKEN` | `0` | 是否兼容旧的 `?token=` 传参 |
| `TZ` / `HTTP_PORT` | `Asia/Shanghai` / `10307` | 时区 / 宿主机端口 |

完整说明见 `SECURITY.md`；`.env` 各字段获取方式见 **第四节 4.1**。
