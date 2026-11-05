# 安全说明

## 威胁模型

本发行版针对的是「把 emby-pulse 通过 Cloudflare Tunnel / 反向代理暴露到公网」这一部署形态。在该形态下，浏览器页面是 https，而回源是 http，代理头处理不当会同时影响功能正确性与凭证边界。

**两条链路要分开看**：管理端 / 用户端是「浏览器 ↔ 公网反代」链路，适用上面的公网威胁模型；而 **Webhook 回调是「Emby Server ↔ EMBYPULSE」的内网直连**（`http://<内网IP>:10307/api/v1/webhook?token=...`），由服务端直接发起，不经过浏览器、CDN 或公网反代，因此不存在浏览器历史、书签、Referer、公网反代日志这些凭证扩散面。这也是 1.9.0 起可以默认兼容 `?token=` 的前提。管理端暴露到公网，不等于 Webhook 回调经过公网。

### 本次修复覆盖

| 风险 | 处理 |
|---|---|
| 反代下 CSRF Origin 误判导致写接口不可用 | `FORWARDED_ALLOW_IPS` + origin 变体兜底 |
| Webhook token 出现在 URL，被日志/历史/Referer 记录 | 管理端响应一律脱敏、页面不写死 token；1.9.0 起 Webhook 走内网直连且 Emby 插件只能配 URL，故默认兼容 `?token=`（无浏览器/公网链路泄露面），可选 `WEBHOOK_ALLOW_QUERY_TOKEN=0` 切回 Header-only |
| 路由漏写鉴权即默认放行 | 集中式默认拒绝网关 |
| 配置接口向浏览器回传明文凭证 | 响应字段脱敏 + 模板去 token 拼串 |
| 运行期配置/数据混入代码仓库或镜像 | 白名单发布 + 双重 ignore + CI 强制扫描 |

### 不覆盖 / 需部署方自行负责

- TLS 终止、tunnel 访问控制、Emby 本身权限；
- 宿主机与 Docker 守护进程安全；
- 备份文件、日志文件的落盘权限与留存策略；
- 上游应用未来版本引入的新问题。

---

## `FORWARDED_ALLOW_IPS` 的安全边界

这个变量决定 uvicorn 信任哪些来源的 `X-Forwarded-*`。信任范围过宽，客户端就能伪造 `X-Forwarded-Proto` / `X-Forwarded-For`。

- 默认值只包含回环与 RFC1918 内网段，适用于 cloudflared / 内网反代 / docker-proxy 的常见拓扑；
- **不要**设为 `*`；
- 若反代不在这些网段，只追加该反代的精确地址（把 `<PROXY_IP>` 换成你的实际地址），例如 `FORWARDED_ALLOW_IPS=127.0.0.1,::1,<PROXY_IP>`；
- 该头只影响 `scope` 中的 scheme/client，不改变 CSRF 对 netloc 的严格比较，应用层兜底同样不接受客户端任意指定的 Host。

---

## 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` | uvicorn 信任的代理来源 |
| `AUTH_GATE_MODE` | `enforce` | `enforce` 拦截 / `observe` 只记录 / `off` 关闭 |
| `PUBLIC_API_PATHS` | 空 | 追加放行路径，逗号分隔；谨慎使用 |
| `WEBHOOK_ALLOW_QUERY_TOKEN` | `1` | 兼容 Emby Webhook 插件只能配 URL 的限制（内网直连，无公网泄露面）；设 `0` 回到严格 Header-only |
| `EMBY_HOST` | `http://127.0.0.1:8096` | Emby 地址 |
| `TZ` | `Asia/Shanghai` | 时区 |
| `HTTP_PORT` | `10307` | 宿主机端口（compose） |

### 灰度建议

首次切换到本镜像时，可先设 `AUTH_GATE_MODE=observe` 观察一个业务周期，确认没有内部脚本依赖原先放行的接口，再改为 `enforce`。其余修复无需灰度。

---

## 凭证处理

- Webhook 凭证优先走 `X-Webhook-Token` 请求头；**1.9.0 起默认同时兼容 `?token=` URL**，因为 Emby Webhook 插件无法附带自定义请求头。后端仍不向前端回传明文 Token，设置页也绝不把脱敏值拼进地址。
- **兼容 `?token=` 的残余风险（内网拓扑下很低）**：Webhook 是内网服务端直连，凭证不会进入浏览器历史 / 书签 / Referer / 公网反代日志；唯一残留是 **EMBYPULSE 本机的 uvicorn 访问日志**（记录带 query 的路径），只在内网设备本地。只要 10307 不对公网暴露且本机日志不外发，无需额外处理，也无需为此改用 Header。
- **改变拓扑时才需要收紧**：若日后把 `/api/v1/webhook` 暴露到公网、改走公网反向代理、或把访问日志上报到第三方，则应收紧日志中 query 的采集与留存，并考虑把 `WEBHOOK_ALLOW_QUERY_TOKEN` 设为 `0`（前提是媒体服务器插件支持自定义请求头）。
- Bark Device Key 只在运行期配置中保存原文，管理端读取与推送返回值/日志一律掩码或剔除；回填掩码值不会覆盖真实 Key。
- API 调用走 `Authorization: Bearer <API Token>`；
- 会话 cookie 保持 `HttpOnly` / `Secure` / `SameSite=strict`（由应用自身设置）；
- 任何真实凭证只应存在于运行期卷与本机 `.env`，绝不写入仓库文件；
- 若曾经把凭证贴进过对话、日志或提交信息，视为已泄露，应立即轮换。

---

## 升级与回滚

1. 升级前备份 `ep-data` 命名卷（内含数据库）；
2. 修改源码或 tag 后重跑 CI，确认 `tools/verify_patches.py` 断言与容器冒烟通过再部署；
3. 回滚直接把 compose 的 image 指向上一个可用 tag / digest；
4. 重建容器会清理在线会话，用户需重新登录。

---

## 依赖与合规

- 应用源码来自上游 `amlkiller/emby-pulse` main 分支快照，逐文件按 Git Blob sha 拉取，不含上游提交历史；
- 镜像由本仓库 `Dockerfile` 自行构建，不引用上游镜像；运行时基础镜像为 `python:3.12-slim`，依赖由 `uv.lock` 锁定；
- 当前 CI 构建 `linux/amd64` 镜像；ARM 设备可在本地执行 `docker build -t emby-pulse .` 自行构建；
- 上游仓库未附带 LICENSE，请自行评估再分发合规性；
- CI 仅使用 `GITHUB_TOKEN` 推送 GHCR，不需要也不应在仓库中存放任何长期凭证。

---

## 报告问题

请通过仓库 Issue 提交，附复现步骤与脱敏后的日志。提交前请移除域名、IP、Token、Cookie 等真实值。
