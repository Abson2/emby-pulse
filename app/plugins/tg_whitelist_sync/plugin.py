"""
求片同步 TgtoDrive 关键词白名单插件（v2.0.0）
=============================================

本插件**只负责把求片推送进 TgtoDrive，绝不删除任何规则**。

职责边界（与插件 B 的分工）
---------------------------
- 插件 A（本文件，运行在 EMBYPULSE 侧）：把工单大厅里**审批通过 / 手动接单**的求片，
  按 TMDB 编号写入飞牛上 TgtoDrive 的「光鸭云盘 → 频道监控配置 → 关键词过滤 → 关键词白名单」，
  触发 tg 频道监控入库。**只写不删**：合并写入时完整保留用户已有的其它关键词。
- 插件 B（运行在 TgtoDrive 容器内，仓库 tgtdrive-monitor/）：按「影视已入库 Emby」判定完结，
  完结满 cleanup_days(默认5) 天后，删除本插件写入的 kw_ 前缀关键词。清理权完全交给插件 B。

为什么 A 不删
--------------
早期版本在 EMBYPULSE 侧做"完结清理"，因全量覆盖写会冲掉用户通过 TgtoDrive 页面手动添加的
关键词（坑①）。清理是 TgtoDrive 侧的事，交给插件 B 在 TgtoDrive 容器内执行，避免跨容器误删。

触发方式：事件驱动 + 轻量轮询兜底
---------------------------------
- **事件驱动（主路径）**：工单大厅管理员点「手动接单 / 审批通过」时，management_router 会
  异步调用本插件的 `push_items(candidates, set_status_on_success=1)`，点完立即推送，不等轮询。
  - 审批通过(approve) → status=1（自动下载中），并推送；
  - 手动接单(manual)  → status=4（手动接单中），并推送；推送成功后流转为 status=1（自动下载中）；
  - 审批拒绝(reject)  → 不推送，流程流转为 status=3（已拒绝）。
- **入库流转**由 EMBYPULSE 核心负责：Emby webhook 触发 `auto_finish_request` 把 status 置 2
  （已入库）并通知管理员/用户。本插件不重复实现。
- **轻量轮询兜底**：后台每 check_interval 分钟扫描一次 status∈(1,4) 的工单补推，仅作容错。

推送的每条规则除 {id, title, keywords[]} 外，按配置带筛选字段：
分辨率(rule_resolution, 默认 4k) / 画面效果(rule_effect, 默认 all=全部效果) /
年份(取自 TMDB，自动填写) / 必包含关键词(rule_required_keyword, 默认 265=H.265/HEVC)。

TgtoDrive 写入接口（基于 8.8.27 实测，不走官方 Python 插件路线）
---------------------------------------------------------------
白名单写入是标准 REST 接口，无需改 TgtoDrive 源码：

    POST /api/visual-filter/config

请求体（场景 key 用 `scene` 字段区隔，光鸭为 `ENV_FILTER_GUANGYA`）：

    {
      "scene": "ENV_FILTER_GUANGYA",
      "rules": [ { "id": "...", "title": "...", "keywords": ["关键词"] } ],
      "parse_mode": 1,
      "all_mode": 0,
      "raw_regex": "",
      "advanced_raw_regex": ""
    }

两个必须知道的坑（已在本插件内处理）：
1. **全量覆盖，不是 append**：直接 POST 一条会把现有规则全冲掉。
   所以强制 `GET 当前 scene → 合并 → 整体 POST`，且**完整保留非 kw_ 的用户规则**。
2. **后端信 rules 不信 raw_regex**：绝不手拼 raw_regex，只维护 rules 数组，
   丢弃 GET 回来的陈旧 raw_regex，让后端按 rules 重新编译。

鉴权：优先 `Authorization: Bearer <OpenAPI 令牌>`（系统工具→实用工具→OpenAPI 创建）；
若白名单接口只认网页会话，可用 `tgtdrive_cookie` 配置自签 session（兜底）。

作者: Abson2
"""

import logging
import os
import json
import re
import threading
import time
from datetime import datetime, timezone

import requests
from fastapi import Request

from app.plugins.base import PluginBase
from app.domains.media_requests.media_request_dao import (
    list_all_requests,
    update_media_request_status,
    get_media_request,
)
try:
    from app.infra.clients.media_server_client import media_api
except Exception:  # 部分部署未配置媒体服务器时仍可加载插件
    media_api = None

logger = logging.getLogger("uvicorn")

# 待同步状态集合：下载中(1) / 手动接单(4)
# 注意：不含 0(待审批) —— 需求要求「审批后再触发」，未审批不占用 tg 频道监控；
# 不含 7(待入库) —— 7 表示内容已转存完等待 Emby 入库，入库流转由核心 auto_finish 负责。
PENDING_STATUS = (1, 4)

# 审批通过后 EMBYPULSE 写入的状态码（management_router: approve -> update_status(tid, sn, 1)）
STATUS_APPROVED = 1
# 推送成功后工单流转到的状态（自动下载中）。
STATUS_AUTO_DOWNLOADING = 1

# TgtoDrive visual-filter 白名单写入路径（8.8.27 实测）
VISUAL_FILTER_ENDPOINT = "/api/visual-filter/config"

# 默认场景：光鸭云盘（ENV_FILTER=123 / ENV_FILTER_GUANGYA=光鸭 / ENV_FILTER_115 /
# ENV_FILTER_HDHIVE=RE0 / ENV_FILTER_189=天翼）。本方案目标为光鸭。
DEFAULT_SCENE = "ENV_FILTER_GUANGYA"

# 工单状态码 -> 文本（与 media_request_dao / management_router 保持一致）
STATUS_TEXT = {
    0: "待审批",
    1: "下载中",
    2: "已完成",
    3: "已拒绝",
    4: "手动接单",
    7: "待入库",
}

# 本插件写入的监控规则 id 前缀：插件 B 清理时只动这些，绝不波及用户其他关键词
RULE_ID_PREFIX = "kw_"

# 光鸭白名单「规则对象」字段名映射。
# 注意：TgtoDrive 的 OpenAPI 规范（自动生成）只导出了顶层字段（scene/rules/...），
# 并未导出 rules[] 内部每条规则的字段结构，因此下列字段名是基于 UI 表单推断的。
# 若实际抓包发现字段名不同，仅修改本字典即可，无需改动其它代码。
# 取值（4k / all / 265）由插件配置项提供，字段名在此集中维护。
RULE_FIELD = {
    "resolution": "resolution",      # 分辨率选择（如 4k）
    "effect": "picture_effect",      # 画面效果（"all" 表示全部效果）
    "year": "year",                  # 年份（取自 TMDB）
    "required": "must_include",      # 必包含关键词（如 265 / H.265）
}


class TgtoDriveError(Exception):
    """TgtoDrive 调用失败"""


# Docker 默认桥网关：host 网络模式的 TgtoDrive 从 bridge 网络的 EMBYPULSE 看过去，
# 稳定地址是宿主网关 172.17.0.1，而非 NAS 的 LAN IP（后者会因 DHCP / 换网而变）。
# 注意：本文件位于 app/ 目录，受 check-no-secrets 的 DOMAIN_EXEMPT_TOP 豁免，
# 此处字面量 IP 不会触发凭证扫描。其它目录（如 tgtdrive-monitor/）一律只用 127.0.0.1。
DEFAULT_TGTODRIVE_URL = "http://172.17.0.1:12366"


def _resolve_base_url(cfg):
    """按优先级解析 TgtoDrive 地址，避免把易变的 IP 写死在插件里。

    优先级：
      1. 插件配置 UI 显式填写的 tgtdrive_base_url（最高优先，便于临时覆盖）；
      2. 环境变量 TGTODRIVE_URL（在 docker-compose.yml 一处配置，容器重建不丢）；
      3. 本地配置文件 <dir>/tgtodrive.json 的 base_url（EMBYPULSE 配置目录内）；
      4. 默认值 DEFAULT_TGTODRIVE_URL（docker 宿主网关，host 网络 TgtoDrive 稳定地址）。

    注意：env 与候选目录均在调用时求值，便于容器启动时再注入环境变量。
    """
    ui = (cfg or {}).get("tgtdrive_base_url") or ""
    if str(ui).strip():
        return str(ui).strip()
    env = os.environ.get("TGTODRIVE_URL", "")
    if env.strip():
        return env.strip()
    candidate_dirs = [
        os.environ.get("EMBYPULSE_CONFIG_DIR", ""),
        "/app/config",
        "config",
        ".",
    ]
    for d in candidate_dirs:
        if not d:
            continue
        p = os.path.join(d, "tgtodrive.json")
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                v = (data.get("base_url") or "").strip()
                if v:
                    return v
        except Exception:
            continue
    return DEFAULT_TGTODRIVE_URL


def _row_get(row, key, default=None):
    """从 DAO 返回行中按列名取值，兼容 DataRow / sqlite3.Row / dict / tuple。

    实测 media_request_dao 返回 DataRow（dict 子类，键为列名，大小写不敏感），
    也支持位置下标。但位置下标依赖 SELECT 列序，上游调整列序会静默错位，
    故统一优先按列名取值；仅当拿不到列名时才退回位置下标。
    """
    if row is None:
        return default
    # 1) 按列名（dict 子类 / sqlite3.Row 支持 keys()）
    try:
        if hasattr(row, "keys"):
            for k in row.keys():
                if str(k).lower() == key.lower():
                    return row[k]
            return default
    except Exception:
        pass
    # 2) 序列兜底（tuple/list），只在明确知道列序时才会走到这里
    try:
        if isinstance(row, (tuple, list)):
            return default
    except Exception:
        pass
    return default


def _as_int(value, default=None):
    """宽松转 int：实测 status 是 int(0)，但历史数据/JSON 往返可能是 "0"。

    注意 reject_reason 的 "None" 是旧版本 str() 产物，本函数只用于数值字段。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class TgtoDriveClient:
    """TgtoDrive 8.8.x visual-filter 白名单客户端（基于实测 REST 接口）。

    只依赖 requests，不引入额外包。base_url 默认取 docker 宿主网关
    http://172.17.0.1:12366（TgtoDrive host 网络模式、默认管理端口 12366）。
    地址解析见 _resolve_base_url：UI 配置 > 环境变量 TGTODRIVE_URL >
    config/tgtodrive.json > 默认值，便于容器变动时一处修改即可恢复通信。

    写入策略：GET 当前 scene 配置 → 合并 rules → 整体 POST（全量覆盖）。
    这样绝不会冲掉用户已在页面里配置的其他关键词（**本插件永不删除**）。
    """

    def __init__(self, base_url, token, endpoint, scene=DEFAULT_SCENE,
                 cookie=None, timeout=15, logger=None):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.endpoint = endpoint or VISUAL_FILTER_ENDPOINT
        self.scene = scene or DEFAULT_SCENE
        self.cookie = cookie or ""
        self.timeout = timeout
        self._log = logger

    # -------------------------------------------------------- 基础
    def _url(self):
        if not self.base_url:
            raise TgtoDriveError("未配置 TgtoDrive 地址")
        if not self.endpoint:
            raise TgtoDriveError("未配置白名单写入接口路径")
        ep = self.endpoint if self.endpoint.startswith("/") else "/" + self.endpoint
        return f"{self.base_url}{ep}"

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            # TgtoDrive OpenAPI 使用 Authorization: Bearer <TOKEN>
            h["Authorization"] = f"Bearer {self.token}"
        if self.cookie:
            # 兜底：网页会话 Cookie（如自签 session）
            h["Cookie"] = self.cookie
        return h

    @staticmethod
    def _rule_id(tmdb_id, season):
        """生成白名单规则幂等 id，便于去重与回查。"""
        return f"kw_{int(tmdb_id)}_{int(season or 0)}"

    @staticmethod
    def _safe_id(rule):
        if isinstance(rule, dict):
            return rule.get("id")
        return None

    @staticmethod
    def _find_rules_container(cfg, key="rules"):
        """在 GET 返回里递归定位 rules 列表所在的容器与子键。

        返回 (container_dict, subkey)；找不到时 (None, None)。
        兼容顶层 {rules:[...]}、嵌套 {data:{rules:[...]}} 以及其它任意层级，
        避免因为 TgtoDrive 返回信封结构变化而拿不到现有规则（否则会被当成空场景整体覆盖）。
        """
        if isinstance(cfg, dict):
            if key in cfg and isinstance(cfg[key], list):
                return cfg, key
            for k, v in cfg.items():
                res = TgtoDriveClient._find_rules_container(v, key)
                if res[0] is not None:
                    return res
        return None, None

    @staticmethod
    def _extract_rules(cfg):
        """从 GET 返回里稳妥取出 rules 数组（兼容顶层 / 嵌套结构）。"""
        container, subkey = TgtoDriveClient._find_rules_container(cfg)
        if container is not None:
            rules = container.get(subkey)
            if isinstance(rules, list):
                return rules
        return []

    @staticmethod
    def _strip_raw_regex(cfg):
        """递归删除 raw_regex / advanced_raw_regex 键（后端信 rules 不信正则）。"""
        if isinstance(cfg, dict):
            cfg.pop("raw_regex", None)
            cfg.pop("advanced_raw_regex", None)
            for v in cfg.values():
                TgtoDriveClient._strip_raw_regex(v)
        elif isinstance(cfg, list):
            for v in cfg:
                TgtoDriveClient._strip_raw_regex(v)

    # -------------------------------------------------------- 读
    def get_config(self):
        """读取当前 scene 的完整配置。404 视为空配置。其他错误抛异常。"""
        try:
            resp = requests.get(
                self._url(),
                params={"scene": self.scene},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise TgtoDriveError(f"GET 配置失败: {e}") from e

        if resp.status_code == 404:
            return {}
        if not (200 <= resp.status_code < 300):
            raise TgtoDriveError(
                f"GET 配置 HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError:
            raise TgtoDriveError("GET 配置返回非 JSON 响应")

    # -------------------------------------------------------- 构造单条规则
    def build_rule(self, item):
        """把一条求片构造为白名单规则对象。

        item 字段：tmdb_id / media_type / year / season / title / keyword / rule_fields
        最小可用字段为 {id, title, keywords[]}；额外附 tmdb_id 等便于回查，
        并按 RULE_FIELD 映射写入分辨率/画面效果/年份/必包含关键词等筛选字段。
        后端按 rules 编译正则（坑②），未知字段会被忽略，故字段名以实际抓包为准。
        """
        rid = self._rule_id(item["tmdb_id"], item["season"])
        title = (item.get("title") or "").strip() or f"TMDB{item['tmdb_id']}"
        rule = {
            "id": rid,
            "title": title,
            "keywords": [item["keyword"]],
            "tmdb_id": item["tmdb_id"],
            "media_type": item.get("media_type"),
        }
        # 年份：取自 TMDB（工单提交时即由 TMDB 填充，符合"读取TMDB自动填写"）
        if item.get("year"):
            rule[RULE_FIELD["year"]] = item["year"]
        rf = item.get("rule_fields") or {}
        if rf.get("resolution"):
            rule[RULE_FIELD["resolution"]] = rf["resolution"]
        if rf.get("effect"):
            rule[RULE_FIELD["effect"]] = rf["effect"]
        req = (rf.get("required") or "").strip()
        if req:
            # 必包含关键词的字段名由配置 rule_required_field 决定（默认 must_include，
            # 系依据 TgtoDrive UI 推断；若实际抓包字段名不同，改配置即可，无需动代码）
            rule[item.get("rule_required_field") or RULE_FIELD["required"]] = req
        return rule

    # -------------------------------------------------------- 合并写（核心，永不删除）
    def merge_and_save(self, items):
        """读-合并-写：把 items 追加进当前 scene 白名单，整体 POST 覆盖。

        **关键保证**：只把本插件要写的新规则合并进去，**完整保留所有已存在的规则
        （含用户通过 TgtoDrive 页面手动添加的其它关键词）**，绝不删除任何用户规则。

        安全护栏：若 GET 成功返回但无法从响应中解析出 rules（接口版本 / scene /
        鉴权不匹配导致结构未知），**中止推送**而非冒险覆盖现有规则——宁可本次不写入，
        也绝不让用户已有的频道监控关键词被冲掉。

        返回 dict: {ok, added[], skipped[], changed, detail}
        """
        cfg = self.get_config()

        container, subkey = self._find_rules_container(cfg)
        if container is None:
            # 找不到 rules 容器：区分「真·空场景(404 返回 {})」与「未知结构」
            if cfg:
                raise TgtoDriveError(
                    "GET 返回无法解析出 rules（可能为接口版本 / scene / 鉴权不匹配），"
                    "为安全起见中止推送，避免覆盖现有规则。"
                    "请提供 /api/visual-filter/config 的真实返回样本以便修正字段名/信封结构。"
                )
            # 真正空场景：从零创建
            cfg = {"scene": self.scene, "rules": []}
            container, subkey = cfg, "rules"

        existing = list(container.get(subkey) or [])
        existing_ids = {self._safe_id(r) for r in existing}

        new_rules = []
        added_ids = []
        skipped_ids = []
        seen = set()
        for it in items:
            rid = self._rule_id(it["tmdb_id"], it["season"])
            if rid in existing_ids or rid in seen:
                skipped_ids.append(rid)
                continue
            seen.add(rid)
            new_rules.append(self.build_rule(it))
            added_ids.append(rid)

        if not new_rules:
            return {
                "ok": True,
                "added": [],
                "skipped": skipped_ids,
                "changed": False,
                "detail": "白名单已包含所有目标，无需改动",
            }

        # 坑①：保留所有现有规则（含用户规则），仅追加新的 kw_ 规则 —— 绝不删除任何已有规则
        merged = list(existing) + new_rules
        container[subkey] = merged

        # 坑②：后端信 rules 不信 raw_regex，递归丢弃陈旧正则让其重新编译
        self._strip_raw_regex(cfg)
        cfg["scene"] = self.scene

        try:
            resp = requests.post(
                self._url(),
                json=cfg,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise TgtoDriveError(f"POST 配置失败: {e}") from e

        if not (200 <= resp.status_code < 300):
            return {
                "ok": False,
                "added": [],
                "skipped": [],
                "changed": False,
                "detail": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }

        return {
            "ok": True,
            "added": added_ids,
            "skipped": skipped_ids,
            "changed": True,
            "detail": f"已追加 {len(added_ids)} 条规则",
        }

    # -------------------------------------------------------- 探测
    def health_check(self):
        """探测连通性（GET base_url，只判可达）。"""
        try:
            resp = requests.get(self.base_url, timeout=min(5, self.timeout))
            return True, f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            return False, str(e)

    def test_connection(self):
        """连通性 + 鉴权 + 读取当前场景规则数。返回 (ok, detail, rule_count)。"""
        ok, detail = self.health_check()
        if not ok:
            return False, detail, 0
        try:
            cfg = self.get_config()
        except TgtoDriveError as e:
            # 连通但鉴权/接口异常：区分提示
            return False, f"连通可达但读取配置失败: {e}", 0
        rules = self._extract_rules(cfg)
        return True, f"可达，场景 {self.scene} 当前 {len(rules)} 条规则", len(rules)


class TgWhitelistSyncPlugin(PluginBase):
    id = "tg_whitelist_sync"
    name = "求片同步 TgtoDrive 白名单"
    description = "仅推送：把审批通过/手动接单的求片按 TMDB 写入 TgtoDrive 频道监控白名单（分辨率4k/全部效果/年份TMDB/必含265）；清理交由 TgtoDrive 侧插件"
    icon = "fa-tower-broadcast"
    icon_color = "from-sky-500 to-cyan-500"
    version = "2.0.0"
    author = "Abson2"
    permissions = ["db:read", "db:write", "network:out"]

    def __init__(self):
        super().__init__()
        self._thread = None
        self._running = False
        self._stop_event = threading.Event()
        self._last_run = None
        self._last_result = None
        self._stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}
        self._state_file = None
        self._load_state()
        self._setup_routes()

    # ------------------------------------------------------------ 运行态持久化
    def _resolve_state_file(self):
        """解析状态文件路径（配置目录内）。"""
        if self._state_file:
            return self._state_file
        candidate_dirs = [
            os.environ.get("EMBYPULSE_CONFIG_DIR", ""),
            "/app/config",
            "config",
            ".",
        ]
        for d in candidate_dirs:
            if d and os.path.isdir(d):
                self._state_file = os.path.join(d, "tg_whitelist_sync_state.json")
                return self._state_file
        self._state_file = "tg_whitelist_sync_state.json"
        return self._state_file

    def _load_state(self):
        """从配置目录读回最近运行信息（仅统计与结果，不含任何凭证/个人数据）。"""
        try:
            p = self._resolve_state_file()
            if p and os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    s = json.load(f)
                self._last_run = s.get("last_run", None)
                self._last_result = s.get("last_result", None)
                self._stats = s.get(
                    "stats", {"total": 0, "success": 0, "failed": 0, "skipped": 0}
                )
        except Exception:
            pass

    def _save_state(self):
        """把最近运行信息落盘，便于重启后面板仍可读到历史。"""
        try:
            p = self._resolve_state_file()
            d = os.path.dirname(p)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "last_run": self._last_run,
                        "last_result": self._last_result,
                        "stats": self._stats,
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    f,
                    ensure_ascii=False,
                )
        except Exception:
            pass

    # ------------------------------------------------------------ 配置表单
    def get_config_schema(self):
        return [
            {
                "key": "tgtdrive_base_url",
                "label": "TgtoDrive 地址（留空则用环境变量/本地配置）",
                "type": "text",
                "placeholder": "http://172.17.0.1:12366",
                "hint": "留空时按优先级取：环境变量 TGTODRIVE_URL → config/tgtodrive.json → 默认宿主网关 172.17.0.1:12366（host 网络 TgtoDrive 稳定地址，免写死 LAN IP）",
            },
            {
                "key": "tgtdrive_token",
                "label": "OpenAPI 令牌（Bearer）",
                "type": "password",
                "placeholder": "在 系统工具 → 实用工具 → OpenAPI 创建",
                "hint": "优先用 Bearer 令牌鉴权；留空则改用下方 Cookie",
            },
            {
                "key": "tgtdrive_cookie",
                "label": "兜底会话 Cookie",
                "type": "password",
                "placeholder": "session=... 或自签 session",
                "hint": "当白名单接口只认网页会话时填；留空则用 Bearer。请勿填无关 Cookie",
            },
            {
                "key": "tgtdrive_scene",
                "label": "白名单场景 key",
                "type": "text",
                "placeholder": "ENV_FILTER_GUANGYA",
                "hint": "光鸭=ENV_FILTER_GUANGYA；115=ENV_FILTER_115；天翼=ENV_FILTER_189；HDHIVERE0=ENV_FILTER_HDHIVE",
            },
            {
                "key": "whitelist_endpoint",
                "label": "白名单写入接口",
                "type": "text",
                "placeholder": "/api/visual-filter/config",
                "hint": "TgtoDrive 8.8.x 关键词过滤写入路径，默认 /api/visual-filter/config",
            },
            {
                "key": "rule_resolution",
                "label": "规则·分辨率",
                "type": "text",
                "placeholder": "4k",
                "hint": "写入白名单规则的分辨率筛选，默认 4k（如改 1080p 自行填写）",
            },
            {
                "key": "rule_effect",
                "label": "规则·画面效果",
                "type": "text",
                "placeholder": "all",
                "hint": "画面效果筛选，all=全部效果；按 TgtoDrive 实际枚举填写",
            },
            {
                "key": "rule_required_keyword",
                "label": "规则·必包含关键词",
                "type": "text",
                "placeholder": "265",
                "hint": "规则必须包含的关键词（如 265=H.265/HEVC），留空表示不限制",
            },
            {
                "key": "rule_required_field",
                "label": "规则·必含字段名（高级，默认 must_include）",
                "type": "text",
                "placeholder": "must_include",
                "hint": "TgtoDrive 规则对象中『必包含关键词』对应的字段名，系依据其 UI 推断；若实际抓包字段名不同（如 required_keywords/include），改此处即可，无需改代码",
            },
            {
                "key": "check_interval",
                "label": "轮询兜底间隔（分钟）",
                "type": "number",
                "placeholder": "5",
                "hint": "后台每隔多少分钟扫描一次待推送工单作为兜底，默认 5；事件驱动（工单大厅接单）为主路径",
            },
            {
                "key": "trigger_mode",
                "label": "轮询兜底的触发状态",
                "type": "select",
                "options": [
                    {"value": "approved_and_manual", "label": "审批通过 + 手动接单 — status=1,4（推荐）"},
                    {"value": "approved_only", "label": "仅审批通过 — status=1"},
                    {"value": "all_pending", "label": "全部待同步 — status=1,4,7"},
                ],
                "hint": "仅影响后台轮询兜底；无论此处如何设置，工单大厅点「手动接单/审批通过」都会立即事件驱动推送",
            },
            {
                "key": "dry_run",
                "label": "试运行（不写入不改状态）",
                "type": "toggle",
                "hint": "开启后只记录将要执行的动作，便于首次验证",
            },
            {
                "key": "notify_enabled",
                "label": "启用通知",
                "type": "toggle",
                "hint": "开启后插件运行结果会发送到全局通知",
            },
        ]

    # ------------------------------------------------------------ 生命周期
    def on_enable(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="tg-whitelist-sync"
        )
        self._thread.start()
        logger.info("[求片同步] 插件已启用，后台轮询兜底启动（主路径为事件驱动）")

    def on_disable(self):
        self._running = False
        self._stop_event.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2)
        if not t or not t.is_alive():
            self._thread = None
        logger.info("[求片同步] 插件已禁用")

    # ------------------------------------------------------------ 轮询主循环（仅推送兜底）
    def _loop(self):
        # 启动后稍等，避免与 EMBYPULSE 自身初始化抢资源
        if self._stop_event.wait(30):
            return
        while self._running and self._enabled:
            try:
                self._sync_once()
            except Exception as e:  # 单轮异常不终止线程
                logger.error(f"[求片同步] 轮询异常: {e}", exc_info=True)
                self.log(f"轮询异常: {e}", "error")
            finally:
                self._save_state()

            cfg = self._get_config()
            try:
                interval = max(1, int(cfg.get("check_interval") or 5)) * 60
            except (TypeError, ValueError):
                interval = 300

            if self._stop_event.wait(interval):
                return

    # ------------------------------------------------------------ 同步策略
    @staticmethod
    def _allowed_status(trigger_mode):
        """按轮询兜底触发时点返回允许同步的状态集合。

        默认 approved_and_manual：处理审批通过(1) 与手动接单(4)。
        """
        mode = (trigger_mode or "approved_and_manual").strip()
        if mode == "approved_only":
            return (STATUS_APPROVED,)            # 仅审批通过
        if mode == "all_pending":
            return PENDING_STATUS + (7,)        # 含待入库
        # 默认 approved_and_manual
        return PENDING_STATUS

    # ------------------------------------------------------------ 轮询兜底：扫描待推送工单
    def _sync_once(self):
        """后台轮询兜底：扫描 status∈允许集合 的工单并推送。

        事件驱动（工单大厅接单）是主路径，这里仅作容错补推。
        """
        cfg = self._get_config()
        allowed = self._allowed_status(cfg.get("trigger_mode"))
        candidates = []
        for r in list_all_requests():
            st = _as_int(_row_get(r, "status"))
            if st not in allowed:
                continue
            candidates.append((_row_get(r, "tmdb_id"), _row_get(r, "season")))
        if not candidates:
            self._last_result = "无待同步工单"
            return
        self.push_items(candidates, set_status_on_success=STATUS_AUTO_DOWNLOADING)

    # ------------------------------------------------------------ 事件驱动入口（核心）
    def push_items(self, candidates, set_status_on_success=STATUS_AUTO_DOWNLOADING):
        """事件驱动入口：把给定工单候选（list of (tmdb_id, season)）推送到 TgtoDrive 白名单。

        - 合并写入，**绝不删除**任何已有规则（含用户规则）。
        - 推送成功后把工单置为 set_status_on_success（默认 1=自动下载中）。
        - 推送失败仅记录日志，不改动工单状态（保留 4 手动接单中等管理员重试）。
        - dry_run 开启时只记录不写库、不改状态、不写 TgtoDrive。
        返回 dict: {ok, added[], skipped[], changed, detail}
        """
        if not candidates:
            return {"ok": True, "added": [], "skipped": [], "changed": False,
                    "detail": "无候选工单"}
        cfg = self._get_config()
        base = _resolve_base_url(cfg)
        token = (cfg.get("tgtdrive_token") or "").strip()
        cookie = (cfg.get("tgtdrive_cookie") or "").strip()
        scene = (cfg.get("tgtdrive_scene") or DEFAULT_SCENE).strip()
        endpoint = (cfg.get("whitelist_endpoint") or VISUAL_FILTER_ENDPOINT).strip()
        dry_run = cfg.get("dry_run") in (True, "true", "1", 1)
        if not base or not endpoint:
            msg = "TgtoDrive 地址或白名单接口未配置，跳过推送"
            self._last_result = msg
            logger.info(f"[求片同步] {msg}")
            return {"ok": False, "detail": msg}

        # 规则级筛选字段（取值来自配置，字段名由 RULE_FIELD 映射）
        rule_fields = {
            "resolution": (cfg.get("rule_resolution") or "4k").strip(),
            "effect": (cfg.get("rule_effect") or "all").strip(),
            "required": (cfg.get("rule_required_keyword") or "").strip(),
        }
        # 必包含关键词写进规则对象的「字段名」可配置（TgtoDrive 实际字段名未知时由此修正）
        rule_required_field = (cfg.get("rule_required_field") or RULE_FIELD["required"]).strip() or RULE_FIELD["required"]
        if rule_fields["required"]:
            logger.info(
                f"[求片同步] 必包含关键词将写入字段名 {rule_required_field}={rule_fields['required']!r}"
                f"（如 TgtoDrive 实际字段名不同，请在插件配置『必含字段名』中修正）"
            )

        client = TgtoDriveClient(base, token, endpoint, scene, cookie, logger=logger)

        items = []
        for pair in candidates:
            try:
                tmdb_id = int(pair[0]); season = int(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            try:
                row = get_media_request(tmdb_id, season)
            except Exception as e:
                logger.warning(f"[求片同步] 查工单失败 tmdb={tmdb_id} s={season}: {e}")
                continue
            if not row:
                continue
            title = _row_get(row, "title")
            media_type = _row_get(row, "media_type")
            year = _row_get(row, "year")
            eps = _row_get(row, "episodes") or ""
            keyword = self._build_keyword(title, media_type, year, season, eps)
            if not keyword:
                continue
            items.append({
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "year": year,
                "season": season,
                "title": title,
                "status": _as_int(_row_get(row, "status")),
                "keyword": keyword,
                "rule_fields": rule_fields,
                "rule_required_field": rule_required_field,
            })

        if not items:
            self._last_result = "无可用关键词的候选工单"
            return {"ok": True, "added": [], "skipped": [], "changed": False,
                    "detail": "无可用关键词的候选工单"}

        if dry_run:
            for it in items:
                logger.info(
                    f"[求片同步][试运行] 将写入白名单: keyword={it['keyword']!r} "
                    f"tmdb_id={it['tmdb_id']} type={it['media_type']} season={it['season']} "
                    f"scene={scene} resolution={rule_fields['resolution']} "
                    f"effect={rule_fields['effect']} required={rule_fields['required']}"
                )
            self._last_run = time.strftime("%Y-%m-%d %H:%M:%S")
            self._last_result = f"[试运行] 将处理 {len(items)} 条，未实际写入"
            logger.info(f"[求片同步] {self._last_result}")
            self._save_state()
            return {"ok": True, "added": [], "skipped": [], "changed": False,
                    "detail": "dry_run"}

        # 读-合并-写（绝不删除用户规则）
        try:
            result = client.merge_and_save(items)
        except TgtoDriveError as e:
            self._last_run = time.strftime("%Y-%m-%d %H:%M:%S")
            self._last_result = f"推送失败: {e}"
            self._stats["failed"] += len(items)
            logger.warning(f"[求片同步] {self._last_result}")
            self.log(f"推送 TgtoDrive 失败: {e}", "warning", notify=True)
            self._save_state()
            return {"ok": False, "detail": str(e)}

        if not result["ok"]:
            self._last_run = time.strftime("%Y-%m-%d %H:%M:%S")
            self._last_result = f"推送失败: {result['detail']}"
            self._stats["failed"] += len(items)
            logger.warning(f"[求片同步] {self._last_result}")
            self.log(f"推送 TgtoDrive 失败: {result['detail']}", "warning", notify=True)
            self._save_state()
            return result

        # 推送成功：把工单流转到 自动下载中(status=1)；已在该状态的不变更
        done = 0
        for it in items:
            if it["status"] == set_status_on_success:
                done += 1
                continue
            try:
                update_media_request_status(it["tmdb_id"], it["season"], set_status_on_success)
                done += 1
            except Exception as e:
                logger.error(
                    f"[求片同步] 推送成功但置状态失败 tmdb={it['tmdb_id']} s={it['season']}: {e}"
                )
                self.log(
                    f"推送成功但置状态失败: {it['title']} (TMDB {it['tmdb_id']}, S{it['season']}) — {e}",
                    "error",
                )

        added_n = len(result["added"])
        skipped_n = len(result["skipped"])
        self._last_run = time.strftime("%Y-%m-%d %H:%M:%S")
        self._last_result = (
            f"本轮待处理 {len(items)}，新增写入 {added_n}，已存在跳过 {skipped_n}，"
            f"置状态{set_status_on_success}({STATUS_TEXT.get(set_status_on_success,'')}) {done} 条"
        )
        self._stats["total"] += len(items)
        self._stats["success"] += added_n
        self._stats["skipped"] += skipped_n
        logger.info(f"[求片同步] {self._last_result}")
        if added_n:
            self.log(
                f"已同步 {added_n} 条至 TgtoDrive 白名单并置自动下载中: "
                + ", ".join(it["keyword"] for it in items[:5]),
                "info",
            )
        self._save_state()
        return result

    def push_request(self, tmdb_id, season, set_status_on_success=STATUS_AUTO_DOWNLOADING):
        """便捷方法：推送单条工单。"""
        return self.push_items([(tmdb_id, season)], set_status_on_success)

    @staticmethod
    def _build_keyword(title, media_type, year, season, episodes):
        """构造写入白名单的关键词。

        频道监控走关键词匹配，用影视名做主关键词命中率最高；
        剧集附带季号以免同名电影/剧集串味。
        """
        t = (title or "").strip()
        if not t:
            return t
        if media_type == "tv" and season:
            # 若标题里已含季信息则不重复追加
            if "季" not in t and f"S{season}" not in t:
                return f"{t} S{season}"
        return t

    # ------------------------------------------------------------ HTTP 路由
    def _setup_routes(self):
        @self.router.get("/status")
        async def api_status(request: Request):
            """查看运行状态（管理员）"""
            if not request.session.get("user"):
                return {"status": "error", "message": "未登录"}
            from app.domains.users import public_service as user_service

            if not user_service.is_admin_user(request):
                return {"status": "error", "message": "需要管理员权限"}
            cfg = self._get_config()
            return {
                "status": "success",
                "data": {
                    "enabled": self.enabled,
                    "running": self._running,
                    "thread_alive": bool(self._thread and self._thread.is_alive()),
                    "last_run": self._last_run,
                    "last_result": self._last_result,
                    "stats": self._stats,
                    "configured": bool(
                        _resolve_base_url(cfg)
                        and (cfg.get("whitelist_endpoint") or VISUAL_FILTER_ENDPOINT).strip()
                    ),
                    "base_url": _resolve_base_url(cfg),
                    "scene": (cfg.get("tgtdrive_scene") or DEFAULT_SCENE).strip(),
                    "trigger_mode": cfg.get("trigger_mode") or "approved_and_manual",
                    "allowed_status": list(self._allowed_status(cfg.get("trigger_mode"))),
                    "dry_run": cfg.get("dry_run") in (True, "true", "1", 1),
                },
            }

        @self.router.post("/sync_now")
        async def api_sync_now(request: Request):
            """立即执行一次同步（管理员，轮询兜底同款逻辑）"""
            if not request.session.get("user"):
                return {"status": "error", "message": "未登录"}
            from app.domains.users import public_service as user_service

            if not user_service.is_admin_user(request):
                return {"status": "error", "message": "需要管理员权限"}
            try:
                self._sync_once()
                return {
                    "status": "success",
                    "message": "已执行一次同步",
                    "data": {"result": self._last_result, "last_run": self._last_run},
                }
            except Exception as e:
                logger.error(f"[求片同步] 手动同步失败: {e}", exc_info=True)
                return {"status": "error", "message": str(e)}

        @self.router.post("/test_connection")
        async def api_test_connection(request: Request):
            """测试 TgtoDrive 连通性 + 鉴权 + 当前场景规则数（管理员）"""
            if not request.session.get("user"):
                return {"status": "error", "message": "未登录"}
            from app.domains.users import public_service as user_service

            if not user_service.is_admin_user(request):
                return {"status": "error", "message": "需要管理员权限"}
            cfg = self._get_config()
            base = _resolve_base_url(cfg)
            if not base:
                return {"status": "error", "message": "未配置 TgtoDrive 地址"}
            scene = (cfg.get("tgtdrive_scene") or DEFAULT_SCENE).strip()
            endpoint = (cfg.get("whitelist_endpoint") or VISUAL_FILTER_ENDPOINT).strip()
            client = TgtoDriveClient(
                base,
                (cfg.get("tgtdrive_token") or "").strip(),
                endpoint,
                scene,
                (cfg.get("tgtdrive_cookie") or "").strip(),
            )
            ok, detail, rule_count = client.test_connection()
            return {
                "status": "success" if ok else "error",
                "message": detail,
                "data": {"reachable": ok, "detail": detail, "rule_count": rule_count, "scene": scene},
            }

        @self.router.get("/preview")
        async def api_preview(request: Request):
            """预览当前会被同步的工单（管理员，只读）"""
            if not request.session.get("user"):
                return {"status": "error", "message": "未登录"}
            from app.domains.users import public_service as user_service

            if not user_service.is_admin_user(request):
                return {"status": "error", "message": "需要管理员权限"}
            cfg = self._get_config()
            allowed = self._allowed_status(cfg.get("trigger_mode"))
            items = []
            for r in list_all_requests():
                st = _as_int(_row_get(r, "status"))
                if st not in allowed:
                    continue
                title = _row_get(r, "title")
                media_type = _row_get(r, "media_type")
                year = _row_get(r, "year")
                season = _row_get(r, "season")
                episodes = _row_get(r, "episodes") or ""
                items.append(
                    {
                        "tmdb_id": _row_get(r, "tmdb_id"),
                        "media_type": media_type,
                        "title": title,
                        "year": year,
                        "status": st,
                        "status_text": STATUS_TEXT.get(st, str(st)),
                        "season": season,
                        # list_all_requests 用 cnt + users（列表接口字段）
                        "request_count": _row_get(r, "cnt"),
                        "requested_by": _row_get(r, "users"),
                        "keyword": self._build_keyword(title, media_type, year, season, episodes),
                    }
                )
            return {"status": "success", "count": len(items), "data": items}


# 模块级实例：discover_plugins() 会优先识别这个变量
plugin = TgWhitelistSyncPlugin()
