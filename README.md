# 🛡️ emby-pulse 安全加固独立版（基于 EmbyPulse）

<div align="center">
  <img src="static/img/logo-light.png" alt="EmbyPulse Logo" width="180" />

  <h3>Emby 服务器的高级伴侣：数据洞察、全能管理与下载自动化助手 · 安全加固独立发行版</h3>

  <p align="center">
    <a href="https://github.com/Abson2/emby-pulse"><img src="static/img/github-mark.svg" alt="GitHub" width="16" height="16" valign="middle" /> 本项目仓库</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/zeyu8023/emby-pulse">源项目 1 · zeyu8023/emby-pulse</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/amlkiller/emby-pulse">源项目 2 · amlkiller/emby-pulse</a>
  </p>
</div>

> **镜像**：`ghcr.io/abson2/emby-pulse-hardened:latest`
> **仓库性质**：独立公开仓库（非 fork、无 upstream 同步），源码 / Dockerfile / compose / CI 全自持。
> **配置隔离**：仓库不含运行期配置、数据库、密钥、域名或个人数据，CI 首步强制扫描。
> **版本说明**：本页只列功能与用法；各版本的详细更新说明见 [CHANGELOG](CHANGELOG.md) 与 [GitHub Releases](https://github.com/Abson2/emby-pulse/releases)。

---

## 一、原项目继承的内容（功能溯源与鸣谢）

代码源自以下两个上游项目，**完整保留其全部业务能力**。

| 源项目 | 说明 |
|--------|------|
| [zeyu8023/emby-pulse](https://github.com/zeyu8023/emby-pulse)（EmbyPulse 映迹） | 原始项目：仪表盘、缺集、求片、用户中心、数据洞察、映迹工坊 |
| [amlkiller/emby-pulse](https://github.com/amlkiller/emby-pulse)（EmbyPulse-next） | 本发行版直接继承的代码基线：Pro 特性与插件系统 |

> 感谢 [@zeyu8023](https://github.com/zeyu8023) 与 [@amlkiller](https://github.com/amlkiller) 两位作者的开源成果。

### 核心功能

| 功能 | 说明 |
|------|------|
| 全景仪表盘 | 实时播放监控、IP 归属地、资源概览、趋势图表 |
| 缺集管理 | 比对 Emby 库存与 TMDB，联动 MoviePilot 一键搜索 |
| 追剧日历 | 全球热播更新时间表，红绿灯标识入库状态 |
| 求片系统 | 电影 / 剧集 / 季度请求，状态闭环与通知 |
| 用户中心 | 账号信息、到期时间、VIP 标识、用户画像 |
| 数据洞察 | 趣味成就、画质审计、低质筛选 |
| 映迹工坊 | 多维报表与多主题长图生成、推送 |
| 邀请注册 | 邀请链接、自助注册、到期与批量续期 |
| Telegram 机器人 | 播放 / 入库推送、风控告警、交互指令 |
| 系统运维 | 可视化任务、实时日志、数据库工具、系统诊断 |
| 播放历史 | 全站播放流水、IP 归属地、设备信息 |

### Pro 独占特性

影巢资源集成与 115 转存 · 风险管控引擎 · 积分与签到商城 · 自定义通知模板 · 智能合集 · 保号规则 · 邮件系统 · 媒体去重

### 插件系统

影巢资源 · 115 网盘 · 保号规则 · 通知模板 · 智能合集（均可在「插件中心」启停与配置）

---

## 二、本项目新增功能

| 能力 | 说明 |
|------|------|
| 安全加固 | 默认拒绝的 API 鉴权网关、CSRF 反代头兼容、CSP nonce、10307/10308 会话隔离、凭证与日志脱敏、CI 凭证扫描 |
| 入库事件兼容 | Emby 原生 `ItemAdded` / `LibraryChanged` 归一化、`ItemsAdded` 拆条、5 分钟去重 |
| 系统级 Bark 推送 | 管理端独立通道，Device Key 支持 `BARK_DEVICE_KEY` 环境变量（仅管理端） |
| 用户端通知设置 | 「我的 → 通知设置」：Bark / Webhook 二选一，四类内容开关，推送不含网址 |
| 管理端通知渠道 | 七类事件的 Bark 渠道开关、工单拒绝按钮、移动端批量栏横滑 |
| 求片 → TgtoDrive 同步 | 审批通过写入关键词白名单、删单联动移除；配套 TgtoDrive 侧清理插件 |
| 公告与消息 | 富文本净化、历史数据兼容、用户级纯文本推送 |
| 插件日志管控 | 单条 2000 字 / 单插件 500 行 / 留存 30 天 |
| 架构与安全性审查 | 低风险重构、安全契约与等价性回归，见 [docs/REFACTOR_2.0.md](docs/REFACTOR_2.0.md) |

> 每一项能力的版本出处、根因与修复细节都在 [CHANGELOG](CHANGELOG.md) 对应版本中，主页不展开。

---

## 三、使用说明

### 3.1 快速部署

```bash
cp .env.example .env
```

编辑 `.env`（已忽略，不进仓库 / 镜像）：

```bash
EMBY_HOST=http://<Emby局域网IP>:8096   # 本机部署填 http://127.0.0.1:8096
EMBY_API_KEY=                          # Emby 后台「控制台 → API 密钥 → 新建密钥」
SECRET_KEY=                            # 必填：openssl rand -hex 32；留空每次重启掉登录

# 按需启用（留空即不启用）
# TG_BOT_TOKEN=                        # Telegram 机器人
# TMDB_API_KEY=                       # 缺集扫描 / 合集同步
# WEBHOOK_TOKEN=                      # Webhook 令牌：openssl rand -hex 16
# BARK_DEVICE_KEY=                    # 管理端系统级 Bark（不作用于用户端）

TGTODRIVE_URL=http://<DOCKER_HOST_GATEWAY>:12366   # 用同步插件时必填；勿填 NAS LAN IP
TZ=Asia/Shanghai
FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
AUTH_GATE_MODE=enforce                # enforce 拦截 / observe 只记录 / off 关闭
WEBHOOK_ALLOW_QUERY_TOKEN=1           # Emby Webhook 插件只能配 ?token= URL
```

### 3.2 镜像与版本锁定

默认拉取 `2.4`；在 `.env` 里设 `IMAGE_TAG` 可指定历史版本（如 `IMAGE_TAG=1.9.10`）或 `latest`。已发布版本见 [Releases](https://github.com/Abson2/emby-pulse/releases)。

```yaml
services:
  embypulse-pro:
    image: ghcr.io/abson2/emby-pulse-hardened:${IMAGE_TAG:-2.4}
    container_name: embypulse-pro
    restart: unless-stopped
    ports:
      - "10307:10307"   # 管理端（建议仅内网）
      - "10308:10308"   # 用户端（可对外）
    volumes:
      - ./config:/workspace/config
      - ./data:/workspace/data
    env_file:
      - ./.env
```

### 3.3 启动与端口

```bash
docker compose up -d
```

只开放注册 / 求片时仅暴露 `10308`，`10307` 留在内网更安全。[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) 等反代回源指向 `http://<宿主机>:10307`（或 `10308`），tunnel 侧无需额外配置请求头。

### 3.4 环境变量速查

| 变量 | 默认 | 作用 |
|---|---|---|
| `EMBY_HOST` / `EMBY_API_KEY` / `SECRET_KEY` | 空 | Emby 连接与会话密钥，`SECRET_KEY` 务必随机 |
| `TG_BOT_TOKEN` / `TMDB_API_KEY` / `WEBHOOK_TOKEN` / `BARK_DEVICE_KEY` | 空 | 按需启用，支持 env 注入且不落盘 |
| `TGTODRIVE_URL` | 空 | 同步插件地址，host 网络下填宿主桥网关 `:12366` |
| `FORWARDED_ALLOW_IPS` | 内网段白名单 | 允许 uvicorn 采信反代协议头 |
| `AUTH_GATE_MODE` | `enforce` | 鉴权网关模式 |
| `PUBLIC_API_PATHS` | 空 | 追加放行路径，逗号分隔 |
| `WEBHOOK_ALLOW_QUERY_TOKEN` | `1` | `0` = 仅允许 `X-Webhook-Token` 请求头 |
| `TZ` / `HTTP_PORT` | `Asia/Shanghai` / `10307` | 时区 / 管理端口 |

完整说明见 [SECURITY.md](SECURITY.md)；同步插件参数见 [docs/tg-whitelist-sync-design.md](docs/tg-whitelist-sync-design.md)。

### 3.5 Emby Webhook 回调

在 Emby Webhooks 插件中填写 `http://<内网IP>:10307/api/v1/webhook?token=<WEBHOOK_TOKEN 原值>`，完整地址可在「通知与机器人 → 推送场景总控 → 新资源入库通知」一键复制。

- 插件中勾选 **Item Added** 与 **Library Changed**；系统自动归一化事件名、拆条 `ItemsAdded` 并去重，两个都勾不会重复推送。
- 「测试」事件只验证端点与令牌，不产生通知；排查看容器日志是否有 `🔔 触发事件: item.added`。
- 非入库的系统 / 测试事件（如 `LibraryDeleted`）静默忽略，可放心不管。

### 3.6 用户端通知设置

用户访问 `http://<宿主机>:10308` 登录后进入「我的 → 通知设置」：选 **Bark 链接**或 **Webhook**，打开新片入库 / 管理员消息 / 全站公告 / 心愿状态变动四类开关，保存即生效。推送内容不含网址，单个用户推送失败不影响业务主流程。升级后若点击无反应，硬刷新（Ctrl/Cmd+Shift+R）清理旧缓存。

### 3.7 从源码构建

```bash
git clone https://github.com/Abson2/emby-pulse.git
cd emby-pulse
cp .env.example .env
docker compose up -d
```

改源码后自建镜像：把 `image:` 换成自己的镜像名，或修改 GitHub Actions 的推送目标（CI 步骤见 `SECURITY.md`）。

---

## 四、许可证与开源协议

本项目基于 MIT 许可证开源，但必须遵守以下附加条款：

- **强制开源**：任何基于本项目进行的二次开发、修改或衍生作品，必须保持开源，并使用同样的协议发布。
- **保留版权**：修改后的作品必须在显著位置保留原项目 EmbyPulse（映迹）的版权声明及仓库链接（项目 1：`https://github.com/zeyu8023/emby-pulse`；项目 2：`https://github.com/amlkiller/emby-pulse`）。
- **商业禁令**：严禁将本项目及其修改版进行闭源封装、捆绑销售或任何形式的非法盈利。

<div align="center">
  <sub>Designed & Developed by EmbyPulse Team · 安全加固独立发行版 by Abson2</sub>
</div>
