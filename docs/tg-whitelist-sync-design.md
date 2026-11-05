# EMBYPULSE 求片 → TgtoDrive 关键词白名单 → 工单完结：最轻量化方案设计

> 目标：把 EMBYPULSE 工单大厅里**已审批通过**的求片需求（以 TMDB 编号为唯一标识）自动写入飞牛上 TgtoDrive 8.8.27 的「频道监控 → 关键词过滤 → 关键词白名单」，触发 tg 频道监控入库，再把对应 EMBYPULSE 求片工单置为「完结（status=2）」。
>
> 约束：TgtoDrive 源码不可改，只能通过其 OpenAPI（或页面配置）；EMBYPULSE 可改/可加功能；也可另起中间项目。

---

## 零、业务时点（重要，2026-11-05 补充）

**只在工单审批通过后才触发 TgtoDrive 关键词监控**，用户提交（status=0 待审批）时不动。

EMBYPULSE 审批动作的实现（`management_router.py`）：

```python
elif data.action == "approve":
    # 先向 MoviePilot 下发订阅，再更新状态
    update_status(tid, sn, 1)   # 0(待审批) → 1(下载中/已通过)
```

因此：

| 工单状态 | 含义 | 是否同步 |
| --- | --- | --- |
| 0 | 待审批 | **否**（未审批不占用 tg 频道监控）|
| 1 | 审批通过 / 下载中 | **是（默认唯一触发点）** |
| 3 | 已拒绝 | 否 |
| 4 | 手动接单 | 可配 |
| 7 | 待入库 | 可配 |
| 2 | 已完成 | 否（已同步，天然去重）|

插件配置项 `trigger_mode` 提供四档：

| 取值 | 处理状态 | 说明 |
| --- | --- | --- |
| `approved_only`（默认）| 1 | 仅审批通过后触发 |
| `approved_and_manual` | 1, 4 | 审批通过 + 手动接单 |
| `all_pending` | 1, 4, 7 | 待同步全集（仍不含未审批）|
| `include_unapproved` | 0, 1, 4, 7 | 含未审批，**不推荐** |

---

## 一、结论先行（推荐方案）

**推荐：思路 1 —— 在 EMBYPULSE 内新增一个插件（`app/plugins/tg_whitelist_sync/plugin.py`），定时轮询待处理工单，调 TgtoDrive OpenAPI 写关键词白名单，再回写工单状态。**

理由（为什么这是三条里最轻的）：

| 维度 | 思路1：EMBYPULSE 插件（推荐） | 思路3：独立中间项目 | 思路2：TgtoDrive OpenAPI（必选通道，不是独立方案） |
| --- | --- | --- | --- |
| 读取 EMBYPULSE 工单 | 直接 import 内部 DAO，无需 HTTP/登录态 | 需先登录拿管理员 session cookie，脆弱 | — |
| 改 EMBYPULSE 工单状态 | 直接调 `update_media_request_status` | 需再走一遍管理员 HTTP 接口 | — |
| 调 TgtoDrive | HTTP（必然，所有方案都要） | HTTP（必然） | HTTP（必然） |
| 部署形态 | 放进现有镜像，随容器启停，零额外进程 | 多一个容器/进程，要保活、配网络 | — |
| 维护面 | 1 处代码、1 个配置页 | 2 处代码 + 跨容器网络 | — |
| 复用既有能力 | 插件框架已支持定时线程 + 配置表单 + 日志 | 全部自建 | — |

思路 2（TgtoDrive OpenAPI）不是"三选一"的替代项，而是**所有方案都必然用到的下游通道**——因为 TgtoDrive 不可改源码，写白名单只能走它的 OpenAPI/页面。真正要选的是"谁来当协调者"：插件（思路1）还是中间项目（思路3）。插件胜在零额外部署、直接吃内部 DAO、不用处理 EMBYPULSE 的 session 鉴权，明显更轻。用户原话把中间项目列为"最坏的办法"，与代码事实一致。

---

## 二、推荐方案架构与数据流

```
┌─────────────────────────────── EMBYPULSE 容器（现有） ───────────────────────────────┐
│                                                                                     │
│  工单大厅（用户提交求片）                                                             │
│       │  media_requests 表（tmdb_id, media_type, title, season, status）              │
│       ▼                                                                              │
│  ┌────────────────────────────────────────────────────────────┐                      │
│  │ 插件 tg_whitelist_sync（新增）                                │                      │
│  │   ① 后台守护线程，每 N 分钟轮询                                │                      │
│  │   ② list_all_requests() 过滤 status∈(0,1,4,7) 取待同步工单     │                      │
│  │   ③ 对每个工单：提取 tmdb_id / title / media_type / year       │                      │
│  │   ④ 调 TgtoDrive OpenAPI 把「标题(+TMDB号)」加入关键词白名单    │                      │
│  │   ⑤ 成功则 update_media_request_status(tmdb_id,season,2) 完结  │                      │
│  └────────────────────────────────────────────────────────────┘                      │
└───────────────────────────────────────┬─────────────────────────────────────────────┘
                                        │ HTTPS/HTTP（Bearer Token）
                                        ▼
┌─────────────────────────────── TgtoDrive 8.8.27 容器（现有，host:12366） ───────────┐
│   频道监控 → 关键词过滤 → 关键词白名单（新增条目）                                     │
│       → 匹配 tg 频道消息 → 触发光鸭云盘入库                                           │
└──────────────────────────────────────────────────────────────────────────────────┘
```

去重机制：第 ⑤ 步把工单置为 status=2（已完成）后，它自然不再落在 `status∈(0,1,4,7)` 的待同步集合里，**无需额外去重表**。

---

## 二（补充）、通信地址健壮性：不把易变 IP 写死

（详见插件 `_resolve_base_url`，2026-11-05 落地）

TgtoDrive 是 `network_mode: host`，**不在**任何用户自定义 bridge 网络上，
因此 EMBYPULSE（bridge 网络）**无法**用 compose 服务名做 DNS 解析去连它。
从 bridge 容器看 host 网络的 TgtoDrive，稳定地址是**宿主网关（docker0 默认桥网关，
典型为 172.17.x.1 这类地址）**，而不是 NAS 的 LAN IP（会因 DHCP / 换网而变）。

插件对地址做多层解析，优先级从高到低：

1. 插件配置 UI 显式填写的 `tgtdrive_base_url`（便于临时覆盖）；
2. 环境变量 `TGTODRIVE_URL`（在 `.env` 或 `docker-compose.yml` 的 `environment:` 一处配置，容器重建不丢）；
3. 本地配置文件 `<dir>/tgtodrive.json` 的 `base_url`（`<dir>` 取 `EMBYPULSE_CONFIG_DIR` → `/app/config` → `config` → `.`，即 EMBYPULSE 的 bind mount 配置目录，容器重建不丢）；
4. 默认值 `http://<DOCKER_HOST_GATEWAY>:12366`（插件内置，指向宿主桥网关；按你环境实际网关填写，通常为 172.17.x.1 段）。

> 注：仓库与镜像不包含任何内网地址。真实地址写在 NAS 本地 `.env`（已被 gitignore，
> 不入库、不进镜像）；插件默认值已指向宿主桥网关，单机 Docker 上零配置即可工作。

**推荐落地（任选其一，都只改一处即可让容器变动后恢复通信）：**

方案 A（最干净，推荐）—— 写入 NAS 本地 `.env`（compose 已通过 `env_file` 注入）：
```yaml
# .env（NAS 本地，不入库）
TGTODRIVE_URL=http://<DOCKER_HOST_GATEWAY>:12366   # 宿主桥网关:端口（典型 172.17.x.1:12366）
```
如需把网关 IP 也解耦成固定主机名，可在 compose 加 `extra_hosts`，再把上面的地址换成该主机名：
```yaml
# docker-compose.yml（仅别名，不写真实 IP）
# extra_hosts:
#   - tgto123:<DOCKER_HOST_GATEWAY>
# 然后 .env: TGTODRIVE_URL=http://tgto123:12366
```

方案 B —— EMBYPULSE 配置目录内放 `tgtodrive.json`（bind mount，重建不丢）：
```json
{ "base_url": "http://<DOCKER_HOST_GATEWAY>:12366" }
```
路径对应 compose 里的 `./config:/app/config`，即容器内 `/app/config/tgtodrive.json`。

> 注意：TgtoDrive 端口若改过（非 12366），改上面这一处即可；若 TgtoDrive 迁移到
> 另一台宿主机，则需把地址改为那台宿主可达的 IP/域名。想要"连服务名都不用记"的
> 终极解，需把 TgtoDrive 从 host 网络改为同一 user-defined bridge 网络并暴露端口，
> 但这属于改动 TgtoDrive 部署方式，需另行验证其 tg 监控绑定是否受影响。

---

## 三、EMBYPULSE 侧实现要点（代码级，已对源码核实）

### 3.1 可读/可写的内部能力（来自 `app/domains/media_requests/media_request_dao.py`）

- `list_all_requests()` → 全量工单原始行，列序：
  `r[0]=tmdb_id, r[1]=media_type('movie'/'tv'), r[2]=title, r[3]=year, r[4]=poster_path, r[5]=status, r[6]=season, r[7]=created_at, r[8]=request_count, r[9]=requested_by, r[10]=reject_reason, r[11]=episodes, r[12]=request_type, r[13]=series_id`
  → 插件里 `filter(lambda r: r[5] in (0,1,4,7), rows)` 即得待同步工单，且直接拿到 `title(r[2])`、`tmdb_id(r[0])`、`season(r[6])`、`media_type(r[1])`。
- `list_pending_sync_requests()` → 已按 `status IN (0,1,4,7)` 过滤，但**不含 title**，需再 `get_media_request(tmdb_id, season)` 取标题；不如直接用 `list_all_requests()` 过滤省事。
- `update_media_request_status(tmdb_id, season, status, reject_reason=None)` → 单条改状态。置完结传 `status=2`。
- `finish_media_requests_for_item(tmdb_id, season=None)` → 把 `status IN (0,1,4,7)` 整条置 2（适合"一个 tmdb 多个 season 一起完结"）。
- 状态码：`0=待审批, 1=下载中, 2=已完成, 3=已拒绝, 4=手动接单, 7=待入库`（management_router.py L505）。

### 3.2 插件框架（来自 `app/plugins/__init__.py` + `base.py` + `auto_expire/plugin.py`，已核实可用）

- **零配置接入**：在 `app/plugins/` 下新建目录 `tg_whitelist_sync/`，放 `plugin.py`，模块底部 `plugin = TgWhitelistSyncPlugin()`。启动时 `discover_plugins()` 自动扫描注册，无需改任何其他文件。
- **基类能力**（`PluginBase`）：
  - `self.router`：自动带前缀 `/api/plugins/<id>` 的 FastAPI 路由，可挂配置页/手动触发接口。
  - `self.get_config_schema()` / `self._get_config()`：声明配置表单（text/number/toggle/select），配置持久化到 SQLite（`plugin_dao`），自带缓存。
  - `self.log(msg, level, notify)`：写插件日志表 + 可选全局通知。
  - `self.enable()` / `on_enable()` / `on_disable()`：生命周期钩子。
- **定时执行范式**（直接照搬 `auto_expire`）：`on_enable()` 起一个 `threading.Thread(daemon=True)` 跑轮询循环，循环体内 `self._stop_event.wait(interval)` 控制间隔。插件进程就是 EMBYPULSE 进程，线程随容器启停。
- **发 HTTP 到 TgtoDrive**：`requests` 已是项目依赖（tmdb_client / cloud115_client 都用），插件里直接 `import requests` 即可，无新增依赖。
- **线程安全**：复用既有 DAO 函数（它们自己管理 SQLite 连接），不要在插件里另开连接，避免锁竞争。auto_expire 已从线程里调 `user_dao` 证明可行。

### 3.3 插件骨架（伪代码，落地时填实）

```python
import threading, datetime, requests
from app.plugins.base import PluginBase
from app.domains.media_requests.media_request_dao import (
    list_all_requests, update_media_request_status,
)
from app.domains.users import public_service as user_service

PENDING = (0, 1, 4, 7)  # 待审批/下载中/手动接单/待入库

class TgWhitelistSyncPlugin(PluginBase):
    id = "tg_whitelist_sync"
    name = "求片同步 TgtoDrive 白名单"
    description = "把待处理求片按 TMDB 编号写入 TgtoDrive 频道监控关键词白名单，并置工单完结"
    version = "1.0.0"
    author = "Abson2"
    permissions = ["db:read", "db:write", "network:out"]

    def __init__(self):
        super().__init__()
        self._thread = None
        self._running = False
        self._stop = threading.Event()
        self._setup_routes()

    def get_config_schema(self):
        return [
            {"key":"tgtdrive_base_url","label":"TgtoDrive 地址","type":"text","placeholder":"http://<DOCKER_HOST_GATEWAY>:12366","hint":"飞牛上 TgtoDrive 管理页地址（host 网络下填宿主桥网关:端口）"},
            {"key":"tgtdrive_token","label":"OpenAPI 令牌","type":"text","hint":"系统工具→实用工具→OpenAPI 创建"},
            {"key":"whitelist_endpoint","label":"白名单接口路径","type":"text","hint":"从 OpenAPI 页抄，如 /api/openapi/monitor/whitelist"},
            {"key":"check_interval","label":"轮询间隔(分钟)","type":"number","placeholder":"5"},
            {"key":"only_pending_approve","label":"只处理待审批(0)","type":"toggle","hint":"关则处理所有待同步(0/1/4/7)"},
            {"key":"mark_status","label":"提交后工单状态","type":"select","options":[{"value":2,"label":"完结(2)"},{"value":7,"label":"待入库(7)"}],"hint":"默认完结；若想等入库确认再完结可选待入库"},
            {"key":"notify_enabled","label":"启用通知","type":"toggle"},
        ]

    def on_enable(self):
        self._stop.clear(); self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True); self._thread.start()

    def on_disable(self):
        self._running = False; self._stop.set()

    def _loop(self):
        while self._running and self._enabled:
            try: self._sync_once()
            except Exception as e: self.log(f"轮询异常: {e}", "error")
            cfg = self._get_config()
            interval = max(1, int(cfg.get("check_interval") or 5)) * 60
            if self._stop.wait(interval): return

    def _sync_once(self):
        cfg = self._get_config()
        base, token, ep = cfg.get("tgtdrive_base_url"), cfg.get("tgtdrive_token"), cfg.get("whitelist_endpoint")
        if not (base and token and ep):
            self.log("TgtoDrive 未配置完整，跳过本轮", "warning", notify=False); return
        rows = list_all_requests()
        only0 = cfg.get("only_pending_approve") in [True,"true","1",1]
        for r in rows:
            status = r[5]
            if only0 and status != 0: continue
            if status not in PENDING: continue
            tmdb_id, media_type, title, season = r[0], r[1], r[2], r[6]
            ok = self._add_whitelist(base, token, ep, cfg, tmdb_id, media_type, title, r[3])
            if ok:
                mark = int(cfg.get("mark_status") or 2)
                update_media_request_status(tmdb_id, season, mark)
                self.log(f"已同步并置状态{mark}: {title} (tmdb={tmdb_id})", "info")

    def _add_whitelist(self, base, token, ep, cfg, tmdb_id, media_type, title, year):
        # 真实请求体字段需按 TgtoDrive OpenAPI 规范对齐（见第四节待补清单）
        payload = {
            "keyword": title,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "year": year,
        }
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            resp = requests.post(f"{base}{ep}", json=payload, headers=headers, timeout=15)
            if resp.status_code in (200, 201) and resp.json().get("status") in (None, "success", True):
                return True
            self.log(f"白名单添加失败 {resp.status_code}: {resp.text[:200]}", "error", notify=False)
            return False
        except Exception as e:
            self.log(f"调用 TgtoDrive 异常: {e}", "error"); return False

    def _setup_routes(self):
        from fastapi import Request
        @self.router.post("/sync_now")
        async def sync_now(request: Request):
            if not user_service.is_admin_user(request):
                return {"status":"error","message":"需要管理员权限"}
            self._sync_once()
            return {"status":"success","message":"已触发一次同步"}
        # 另可挂 /config 读取配置表单（复用基类 get_config_schema 即可被前端渲染）

plugin = TgWhitelistSyncPlugin()
```

---

## 四、TgtoDrive 8.8.27 侧适配器（已解析 OpenAPI 规范，结论有变）

### 4.1 已确认事实
- 镜像 `walkingd/tgto123:latest`，管理端口 **12366**，`network_mode: host`，登录账号密码由 `ENV_WEB_PASSPORT`/`ENV_WEB_PASSWORD` 注入。
- OpenAPI 入口：**系统工具 → 实用工具 → OpenAPI**，可创建/撤销令牌、按用途搜接口、下载规范、复制调用示例。鉴权：`Authorization: Bearer <TOKEN>`，令牌拥有后台管理员权限。
- **源码与文档不在 GitHub**（`main` 分支无 `docs/`，仅有 `v6.6.4` 旧源码）。

### 4.2 关键结论：OpenAPI 里没有白名单接口 ⚠️

已拿到并完整解析官方规范（`tgto-openapi.json`，OpenAPI 3.0.3，**216 个路径 / 约 227 个操作 / 37 个 tag**）：

| 检查项 | 结果 |
| --- | --- |
| 路径含 `whitelist` | **0** |
| 全文出现「白名单」 | **0** |
| 全文出现「频道」 | **0** |
| 全文出现「监控」 | **0** |
| 全文出现「关键字」 | **0** |

唯一相关的只有 `visual-filter`（可视化筛选规则）4 个接口：

```
GET  /api/visual-filter/config        读取可视化筛选规则   (query: scene)
POST /api/visual-filter/config        保存可视化筛选规则   (body: scene, rules, raw_regex, advanced_raw_regex, all_mode, parse_mode)
POST /api/visual-filter/config/sync    同步筛选规则到其他场景 (body: source_scene, source_raw_regex, target_scene)
GET  /api/visual-filter/tmdb/search   为筛选规则搜索 TMDB 条目
```

其描述为「保存后供 **Telegram 和资源筛选**等相关场景使用」，属于**资源筛选**场景，不是你要的「光鸭云盘 → 频道监控配置 → 关键词过滤 → 关键词白名单」。

规范自身也声明了这层局限（`info.description`）：参数由**构建时静态分析**生成，`inferred` 149 个、`generic` 78 个、只有 16 个 `documented`，**不是完整人工校验的模型**。因此**频道监控模块大概率未被纳入 OpenAPI 导出**，不是导出遗漏。

其他候选入口均不合适：
- `POST /api/env` —— 保存项目配置会**触发容器重启**，不能做实时写入；
- `GET/POST /api/bot-commands` —— 只管机器人命令，与频道监控无关；
- `/api/notify-bots`、`/api/toolbox/tg-scheduled-sender/config` —— 通知/定时发送，非频道监控。

**结论（已过时，见 4.5）：早期解析认为 OpenAPI 无白名单接口；后续实测确认频道监控关键词白名单即 `visual-filter` 场景规则，可通过 `POST /api/visual-filter/config` 写入（见 4.5）。**

### 4.3 改走方案：TgtoDrive 官方 Python 插件机制

TgtoDrive 官方支持「Python 插件」：导入单 `.py` 或 ZIP/PYZ 插件包，带参数表单、定时执行与日志（规范中对应 `/api/toolbox/plugins/*` 一组接口，含 import / run / schedules）。这是**官方扩展点，不属于修改源码**，符合你的约束。

落地路径（推荐顺序）：

1. **首选：确认频道监控页面是否有专用 HTTP 接口。** 在 TgtoDrive 网页打开「频道监控配置」，按 F12 → Network，在关键词白名单里手动加一条词，看请求的 URL / 方法 / 请求体。这是最直接的真实接口，拿到后填入插件配置 `whitelist_endpoint` 即可，**代码零改动**。
2. **次选：写一个 TgtoDrive Python 插件**，在容器内直接读写其白名单配置（`/app/db` 下持久化），再由 EMBYPULSE 插件通过 `/api/toolbox/plugins/{id}/run` 触发。
3. **兜底：** 若白名单只在前端内存、无任何持久化入口，则退回「关键词正则」路线——用 `POST /api/visual-filter/config` 把求片标题写入筛选规则，让资源筛选/Telegram 场景吃掉。

### 4.4 当前插件已就绪的部分

EMBYPULSE 侧插件 `app/plugins/tg_whitelist_sync/plugin.py` 已实现并通过 7 组测试，**TgtoDrive 写入端点做成配置项**（`whitelist_endpoint`），因此上述任一真实端点确定后，只需在插件配置页填路径，**无需改代码**。

> 早期描述中的 `add_keyword()` 已废弃，现统一走 `TgtoDriveClient.merge_and_save()`（读-合并-写）。后续实测结论见 4.5。

---

### 4.5 实测确认：白名单即 visual-filter 场景规则（更新于 2026-11-05）

用户实测拿到真实写入路径与坑，结论推翻 4.2 的"无法写入"判断：

- 写入接口：`POST /api/visual-filter/config`（与读取同 URL，GET 带 `?scene=`）。
- 请求体（顶层）：`{ scene, rules, parse_mode, all_mode, raw_regex, advanced_raw_regex }`。
- 场景 key：光鸭=`ENV_FILTER_GUANGYA`（115=`ENV_FILTER_115`、天翼=`ENV_FILTER_189`、HDHIVE RE0=`ENV_FILTER_HDHIVE`）。
- 每条 `rules[]` 规则对象：最小 `{id, title, keywords[]}`；分辨率/画面效果/年份/必包含关键词等由 UI 表单决定，规范未导出 `rules[]` 内部结构。

三个必须知道的坑（已在插件内处理）：
1. **全量覆盖，不是 append**：直接 POST 一条会把现有规则全冲掉 → 强制 GET 当前 scene → 合并 → 整体 POST（见 `TgtoDriveClient.merge_and_save`）。
2. **后端信 rules 不信 raw_regex**：绝不手拼 raw_regex，只维护 rules 数组，丢弃 GET 回来的陈旧 raw_regex 让其重新编译。
3. **页面"添加"按钮不发请求，只有"保存并回写"才发**：我们后端直接 POST，不走页面按钮，无此坑；单次 POST 即整体落盘。

规则级筛选字段（本次新增，见模块常量 `RULE_FIELD` 与配置项 `rule_resolution`/`rule_effect`/`rule_required_keyword`）：
- 分辨率 `resolution`（默认 `4k`）
- 画面效果 `picture_effect`（默认 `all`=全部效果）
- 年份 `year`（取自 TMDB，自动填写）
- 必包含关键词 `must_include`（默认 `265`=H.265/HEVC）

> ⚠️ 字段名映射集中在插件模块常量 `RULE_FIELD`；因 OpenAPI 规范未导出 `rules[]` 内部结构，下列名系依据 UI 表单推断。验证方法：在 TgtoDrive 页面手动建一条带这些设置的规则，再 `GET /api/visual-filter/config?scene=ENV_FILTER_GUANGYA` 抓包，核对真实字段名后仅改 `RULE_FIELD` 一处即可。

### 4.6 数据契约校准（基于容器内 DAO 实测，2026-11-05）

实测记录：`tmdb=1507696`《天启Z：新世界》，2026-11-05 05:35:05 由 Abson 提交。

`media_request_dao` 返回的是 **`DataRow`（dict 子类，键为列名，大小写不敏感）**，不是 tuple。因此插件**禁止用位置下标取值**，统一走 `_row_get(row, "列名")`。原注释里的"列序 r0..r13"已作废（实测字段序与注释版不一致，位置取值会静默错位）。

两套接口字段不可混用：

| 接口 | 请求人/次数字段 |
| --- | --- |
| `list_all_requests()`（列表，GROUP BY） | `cnt`（次数）+ `users`（用户名逗号串）|
| `get_media_request(tmdb, season)`（单查） | `request_count` + `requested_by` |
| HTTP `/api/manage/requests` | 额外组装 `request_count`/`requested_by`/`series_name`/`is_update` |

五个数据坑与处理方式：

| # | 坑 | 处理 |
| --- | --- | --- |
| 1 | `poster_path` 存**完整 URL**，非 `/xxx.jpg` | 插件不消费该字段，任何地方都不拼 TMDB 图片 CDN 的 `/t/p/w500` 前缀 |
| 2 | `status` 是 **int**（0），非 `"0"` | 用 `_as_int()` 归一后与整数集合比较 |
| 3 | `reject_reason` 是 **None**，非 `""`/"None" | 不按字符串判空，仅原样透传 |
| 4 | `season`/`episodes` 电影恒存在（0 / ""）| 无条件读取，仅拼关键词时对 tv 追加 `S{n}` |
| 5 | 列表/单查字段名不同 | 各接口按其真实字段名取，不混用 |

---

## 五、关键设计决策（建议默认值，可配置）

1. **何时标"完结"**：默认提交白名单成功后立刻置 `status=2`（贴合你"改成完结状态"的字面要求，且天然去重）。若你担心 TgtoDrive 入库失败，可改配成先置 `7=待入库`，另起一个"入库确认"对账（需 TgtoDrive 提供入库完成查询接口，属进阶项）。
2. **处理范围**：默认处理所有待同步 `status∈(0,1,4,7)`；可配成只处理 `0=待审批`，避免动到已在下载/手动接单的工单。
3. **识别粒度**：以 `tmdb_id` 为主键（天然唯一），同时把 `title` 作为关键词写入白名单——TgtoDrive 频道监控按关键词匹配 tg 消息，`title` 是匹配依据，`tmdb_id` 仅作备注/审计。
4. **幂等与失败**：成功改状态即去重；TgtoDrive 调用失败不改状态、下一轮重试；建议补"列出白名单"接口做精确幂等（同名已存在则跳过添加、直接改状态）。
5. **轮询而非事件钩子**：EMBYPULSE 没有"新工单提交"的事件回调，后台线程定时轮询是最稳最轻的做法（已有 auto_expire 印证）。

---

## 六、落地步骤（待你确认方案后执行）

1. 你提供 TgtoDrive OpenAPI 白名单相关接口（第四节清单），或授权我拉镜像提取规范。
2. 我在 `app/plugins/tg_whitelist_sync/plugin.py` 落地上述插件（含配置表单 + 手动触发接口 + 后台轮询）。
3. 本地/测试环境验证：启插件 → 造一条待审批工单 → 看 TgtoDrive 白名单是否新增 → 工单是否变完结。
4. 合入 `Abson2/emby-pulse` hardened 分支，更新 README/CI 服务名断言（沿用上次惯例），重建镜像、飞牛上 `docker compose up -d` 验证。

---

## 七、待你确认 / 提供

- [ ] 采纳"EMBYPULSE 插件"方案（思路1）？
- [ ] TgtoDrive 白名单接口信息：从你实例 OpenAPI 页抄，或给我飞牛 NAS 的 TgtoDrive 地址:端口（我可取规范），或授权我 `docker pull walkingd/tgto123:latest` 提取内部 API。
- [ ] 完结策略：提交即完结(2) 还是 先待入库(7) 再对账？
- [ ] 处理范围：仅待审批(0) 还是 全部待同步(0/1/4/7)？

---

## 八、管理与可观测性（v1.3.0）

- **管理面板**：插件启用后，EMBYPULSE「插件」页该插件卡片新增「同步面板」按钮。面板展示运行状态（运行中/线程存活/已配置/TgtoDrive 地址/场景/触发时点/累计统计/特性2·特性1 开关/清理期限/各阶段最近结果），并提供四个手动动作：**测试连接 / 立即同步 / 入库流转 / 清理监控**；下方列出「待同步工单预览」。四个动作分别调用 `POST /api/plugins/tg_whitelist_sync/{test_connection,sync_now,ingest_now,cleanup_now}`，均需管理员登录。特性1/特性2 不再"无感运行"，可在界面直接观测与触发。
- **运行态持久化**：每次轮询（阶段 A/B/C）结束后，把最近运行信息写盘到配置目录 `tg_whitelist_sync_state.json`（仅运行统计与最近结果，不含任何凭证/个人数据）。容器重启后面板仍可读到历史，便于排查"为什么没同步/没清理"。
- **状态接口**：`GET /api/plugins/tg_whitelist_sync/status` 返回上述全部字段（含 `last_ingest_run` / `last_cleanup_run` 时间戳），面板与人工排查共用同一数据源。

> 通信地址多层解析（UI > `TGTODRIVE_URL` > `config/tgtodrive.json` > 默认宿主网关）与全量覆盖/raw_regex/只动 `kw_` 前缀规则等约束，见上文第四节，未变。
