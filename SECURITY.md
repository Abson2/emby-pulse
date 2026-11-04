# 安全说明

## 威胁模型

本发行版针对的是「把 emby-pulse 通过 Cloudflare Tunnel / 反向代理暴露到公网」这一部署形态。在该形态下，浏览器页面是 https，而回源是 http，代理头处理不当会同时影响功能正确性与凭证边界。

### 本次修复覆盖

| 风险 | 处理 |
|---|---|
| 反代下 CSRF Origin 误判导致写接口不可用 | `FORWARDED_ALLOW_IPS` + origin 变体兜底 |
| Webhook token 出现在 URL，被日志/历史/Referer 记录 | 改用 `X-Webhook-Token` 请求头，URL 去 token |
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
| `WEBHOOK_ALLOW_QUERY_TOKEN` | `0` | 兼容旧 `?token=`，开启即恢复凭证进 URL 的风险 |
| `EMBY_HOST` | `http://127.0.0.1:8096` | Emby 地址 |
| `TZ` | `Asia/Shanghai` | 时区 |
| `HTTP_PORT` | `10307` | 宿主机端口（compose） |

### 灰度建议

首次切换到本镜像时，可先设 `AUTH_GATE_MODE=observe` 观察一个业务周期，确认没有内部脚本依赖原先放行的接口，再改为 `enforce`。其余修复无需灰度。

---

## 凭证处理

- Webhook 凭证走 `X-Webhook-Token` 请求头；
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
