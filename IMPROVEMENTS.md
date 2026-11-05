# 改进明细：根因、修复与验证

本文逐条说明本次独立版做了什么、为什么这样做、如何自己验证。结论来自对上游源码、现网容器内字节码以及真实 Cloudflare Tunnel 环境的实测。

---

## 0. 为什么是「独立仓库」而不是 fork

fork 会带来三个问题：你的仓库与上游形成仓库级同步关系、上游更新不断制造无意义合并、本次改动淹没在提交历史里看不出来。

因此本版本：

- 保留**完整上游源码**作为可运行、可修改的项目基础，逐文件按 Git Blob sha 拉取，**不引入上游任何提交历史**；
- 建立干净的独立公开仓库：单个 root commit、无 upstream remote、不自动同步；
- 5 项改进**直接落在源码里**，共修改/新增 7 个文件，其余源码保持原样，便于逐行对照；
- Dockerfile / compose / CI 全部重写，自带加固默认值；
- CI 依次执行：凭证扫描 → 补丁断言 → 语法编译 → 镜像构建 → 容器冒烟 → 推送 GHCR，任一失败不产出可用镜像。

改动文件清单：

| 文件 | 对应问题 | 性质 |
|---|---|---|
| `Dockerfile` | 1/2 | 重写 + 默认环境 |
| `docker-compose.yml` | 1/2/4 | 重写 |
| `.env.example` | 全部 | 中性占位符 |
| `app/core/csrf_middleware.py` | 1/2 | 修改 |
| `app/core/auth_gate_middleware.py` | 4 | **新增** |
| `app/bootstrap/middleware.py` | 4 | 修改（注册网关） |
| `app/domains/system/webhook.py` | 3 | 修改 |
| `app/bot/bot.py` | 3/5 | 修改 |
| `templates/settings.html` | 3/5 | 修改 |
| `.github/workflows/docker-publish.yml` | 交付 | 重写 |
| `tools/check-no-secrets.py` | 配置隔离 | **新增** |
| `README.md` / `IMPROVEMENTS.md` / `SECURITY.md` | 交付 | **新增** |

---

## 1 / 2. 外网添加用户失败 + 求片心愿 CSRF Origin 不匹配

### 实测现象

| 场景 | 接口 | 结果 |
|---|---|---|
| 内网 HTTP | `POST /api/manage/user/new` | 422（进入业务校验，正常） |
| 外网 HTTPS | `POST /api/manage/user/new` | 403 `{"detail":"CSRF 验证失败：Origin 不匹配"}` |
| 外网 HTTPS | `POST /api/requests/submit` | 403 同上 |

### 根因

Cloudflare Tunnel 回源链路：

```
浏览器 https://<域名> → Cloudflare Edge → cloudflared（内网）→ http://<NAS-LAN-IP>:10307
```

cloudflared 下发 `X-Forwarded-Proto: https`，但**不下发** `X-Forwarded-Host`。

uvicorn 的 `ProxyHeadersMiddleware` 默认 `forwarded_allow_ips = 127.0.0.1`。请求经 docker-proxy 转发后对端不是回环地址，代理头被丢弃，`scope["scheme"]` 保持 `http`，于是 `request.base_url = http://<域名>/`。

原 `_expected_origins()` 只允许 `str(request.base_url).rstrip("/")`，且**仅当存在 `x-forwarded-host` / `x-original-host` 时**才额外组合 `proto://host`。Tunnel 场景这两个头都没有，补不出 `https://<域名>`；浏览器 `Origin` 是 `https://<域名>`，`_origin_allowed()` 按 `scheme + netloc` 严格比较 → 403。

**两个接口是同一个根因**：所有非安全方法都过这层校验，只是这两个是用户最常触发的写操作。这也解释了为什么"只有外网、只有提交时"才出现。

### 修复

**第一层（网关层，主修复）**：`Dockerfile` 与 `docker-compose.yml` 默认注入

```
FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
```

uvicorn 采信来自本机与内网反代的 `X-Forwarded-Proto: https` 后，`scope["scheme"]` 变为 `https`，`base_url` 与浏览器 `Origin` 一致。这是现网已验证的最小改动。

**第二层（应用层，兜底）**：`app/core/csrf_middleware.py` 的 `_expected_origins()` 增加分支——请求带 `X-Forwarded-Proto` 时，按该协议 + **请求自身 Host** 追加允许项。

安全边界：新增允许项的 netloc 完全取自请求的 `Host`，不取自任何客户端可任意指定的转发头；浏览器 `Origin` 由真实页面地址决定，攻击者无法伪造受害者的 `Origin`。伪造 `http://<域名>` 的对照请求仍被拒绝，说明防护未被削弱。

### 验证结果（真实外网域名，修复后）

```
[1] 管理端-添加用户 https → 422  CSRF PASSED
[2] 用户端-求片提交 https → 200  CSRF PASSED | 请先绑定 Emby 账号
[3] 对照-伪造 http Origin → 403  CSRF BLOCKED
[4] 内网基准 http        → 422  CSRF PASSED
```

422 / 200 都表示**已穿过 CSRF 层进入业务逻辑**，403 表示仍被正确拦截。测试使用临时会话，结束后立即清理，未产生真实用户或求片数据。

自查：

```bash
curl -i -X POST "https://<你的域名>/api/manage/user/new" \
  -H "Content-Type: application/json" \
  -H "Origin: https://<你的域名>" \
  -b "session_id=<登录会话>" -d '{}'
# 期望：422 或业务错误，绝不能是 403 CSRF 验证失败
```

CI 中的对应断言：`tools/verify_patches.py` 检查 `_expected_origins` 含 `X-Forwarded-Proto` 分支、`FORWARDED_ALLOW_IPS` 在 Dockerfile/compose 中均存在，容器冒烟检查伪造 http Origin 仍被拒绝。

---

## 3. 明文传送 token

### 证据

上游 `app/domains/system/webhook.py`：

```python
def _get_webhook_token(request):
    """Header 优先，兼容 Emby Webhook 只能配置 URL 参数的场景。"""
    return request.headers.get("X-Webhook-Token") or request.query_params.get("token")
```

`templates/settings.html` 还把 token 拼进可复制 URL：

```javascript
`${host}/api/v1/webhook?token=${token}`
```

URL query 会被完整记录于：反向代理访问日志、浏览器历史、书签、`Referer` 请求头、错误上报、日志采集系统。这属于凭证明文扩散，HTTPS 无法解决。

### 修复

- `_get_webhook_token()` 默认**只**从 `X-Webhook-Token` 请求头读取，仅当 `WEBHOOK_ALLOW_QUERY_TOKEN=1` 时才回退到 query；
- `app/bot/bot.py` 返回的 `webhook_url` 不再带 token，凭证字段始终脱敏；
- 模板中的 URL 预览改为纯路径 `/api/v1/webhook`。

> **1.9.0 修订（重要）**：上面这条「迁移」建议在实际部署中不可执行 —— Emby Webhook 插件**无法设置自定义请求头**，只能配置一个 URL，导致按纯地址配置后回调一律 401、播放/入库事件完全收不到。因此 1.9.0 把默认策略改为**兼容 `?token=`**：`_get_webhook_token()` 请求头优先、默认回退 query，仅显式 `WEBHOOK_ALLOW_QUERY_TOKEN=0` 才回到严格模式；`Dockerfile` / `compose` / `.env.example` 默认值改为 `1`；设置页给出 Emby 可直接复制的完整地址（脱敏值用占位符兜底，绝不把掩码拼进 URL）。
>
> **安全边界没有回退**：后端响应仍不回传明文 Token、模板不硬编码任何 token、凭证字段一律脱敏。而且本部署的 Webhook 由 Emby 服务端在**内网直连**发起，不经浏览器、CDN 与公网反代，原先「凭证进浏览器历史 / Referer / 公网反代日志」的担忧在这条链路上并不成立；唯一残留是 EMBYPULSE 本机访问日志（内网本地，风险很低）。因此默认兼容 `?token=` 是按真实部署拓扑与插件能力做的正确取舍，不是恢复上游的明文拼串做法。收紧日志的要求仅在把回调改走公网反代或日志外发时才适用（见 `SECURITY.md`）。

---

## 4. 未鉴权 API

### 审计

对上游全部 FastAPI 路由做 AST 静态扫描（统计所有 `@router.get/post/put/delete` 及函数体内是否出现鉴权信号）：

```
total routes: 482
by method: GET 246 / POST 208 / DELETE 22 / PUT 6
routes WITHOUT any auth signal: 49
  └─ /api 下写接口 6 个、读接口 11 个
```

典型无鉴权信号接口：

```
GET  /api/history/stats          GET  /api/pwa/config
GET  /api/stats/latest          GET  /api/pwa/manifest.json
GET  /api/stats/recent_added    GET  /api/pwa/icons
GET  /api/wallpaper             POST /api/pwa/set_user_icon
GET  /api/auth/settings         GET  /api/pwa/user_icon
```

> AST 扫描只能证明"函数体里没有明显鉴权代码"，不等于逐个实测可越权访问，因此按"缺少鉴权信号"表述，不夸大结论。真正要解决的是"漏写即放行"这个结构性缺陷。

### 修复

新增 `app/core/auth_gate_middleware.py`，在 `app/bootstrap/middleware.py` 中插入到 `DatabaseSessionMiddleware` 内层（这样才能读到 `request.scope["session"]`）：

- `/api/**` 默认要求登录，管理员前缀要求管理员会话；
- 白名单集中一处：登录、注册、登出、Webhook、健康检查等；
- 支持 `Authorization: Bearer <API Token>`，复用应用自身的 `verify_api_token()`；
- 401 未登录 / 403 权限不足；
- `AUTH_GATE_MODE` = `enforce` / `observe` / `off`，便于灰度。

设计要点是**默认拒绝**：新增路由忘记写鉴权时，结果是 401 而不是数据泄露。

---

## 5. F12 能看到后台配置

### 证据

上游 `app/bot/bot.py` 在配置文件模式下直接把 `webhook_token` 原值放进 `/api/bot/settings` 响应；设置页模板又把输入框中的 token 实时拼进 URL 预览。结果是任何能打开设置页的人，F12 看 Network 就能拿到服务端凭证；界面做掩码也没用，接口响应仍是明文。

### 修复

- `webhook_token` 无论来自 env 还是 config，一律只返回脱敏值，`webhook_url` 不带 token；
- `templates/settings.html` 的 `updateWebhookPreview()` 改为展示纯 URL，提示改用 `X-Webhook-Token` 请求头。

> **1.9.0 修订**：`updateWebhookPreview()` 现在会在**浏览器本地**拼出 Emby 可用的 `?token=` 地址，但**不引入新的明文泄露**：后端响应依旧不含明文、模板不硬编码 token；界面读到的脱敏值（`****`）一律替换为占位符「你的Token」，只有用户手动填入原始值才拼进地址。F12 能看到的是用户自己刚输入的值，而不是服务端凭证。

---

## 6. 个人配置规避

用户明确要求不能发布个人配置。采取的是**来源隔离 + 白名单生成**，而不是"发布后再删"：

1. 源码通过 GitHub Git Blob API 按 tree 逐 sha 拉取，**不复制任何本地目录**，不读取 NAS 上的 `config.json`、`.db`、`.env`；
2. 生成时移除上游 CI、内部工具元数据（如 `.trellis/`）、示例配置、数据库/备份/日志文件；上游 `.env.example` 里的示例内网 IP 替换为本地回环；
3. Dockerfile 只 `COPY app/ templates/ static/ run.py` 等明确目录，禁止 `COPY . .`；
4. `.gitignore` / `.dockerignore` 双重排除配置、数据、数据库、备份、日志、密钥文件；
5. `tools/check-no-secrets.py` 在 CI 第一步扫描全部入库文件：凭证非空赋值、Token 前缀、私钥头、SQLite 文件头、配置数据文件、具体内网地址、非示例域名（域名/地址检查只作用于交付文件与部署文件，不误伤源码里的第三方 API 域名）；
6. `.env.example` 只含中性占位符；
7. 推送完成后，再从 GitHub API 拉回整棵 tree 与全部 blob 复核一遍。

交付说明中会给出扫描命中数，必须为 0。

---

## 7. 已知边界

- 本仓库不自动跟进上游更新；升级需人工对比后提交。
- 上游仓库未附带 LICENSE，再分发合规性需自行评估。
- `AUTH_GATE_MODE=enforce` 可能让自建脚本调用原本"裸奔"的接口时开始返回 401；先用 `observe` 定位调用方，再补白名单或 Token，不要直接关掉网关。
- 重建容器会清理在线会话，切换镜像后用户需重新登录。
