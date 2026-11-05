"""
求片同步 TgtoDrive 关键词白名单插件（v1.2.0）
=============================================

全生命周期：
- 阶段A：审批通过 → 写入 TgtoDrive 频道监控关键词（已完结工单触发）。
- 阶段B（特性2）：待入库(7) 且 Emby 媒体库已有该影视 → 自动流转为已入库(2)。
- 阶段C（特性1）：已入库(默认 status=2) 且超过清理天数(默认5) → 删除对应 TgtoDrive 关键词监控。


把 EMBYPULSE 工单大厅中**已审批通过**的求片（以 TMDB 编号识别），自动写入飞牛上
TgtoDrive 的「光鸭云盘 → 频道监控配置 → 关键词过滤 → 关键词白名单」，
触发 tg 频道监控入库，再把对应 EMBYPULSE 求片工单置为「完结(status=2)」。

业务时点（重要）
----------------
只处理**审批通过后**的工单。EMBYPULSE 中管理员审批通过会执行
`update_status(tid, sn, 1)`，即 status 由 0(待审批) → 1(下载中)。
因此本插件的默认触发条件是 status == 1，而不是用户提交时的 0。
未审批的工单（status=0）一律不碰，避免未经审核就占用 tg 频道监控。

TgtoDrive 写入接口（基于 8.8.27 实测，不走官方 Python 插件路线）
---------------------------------------------------------------
白名单写入是标准 REST 接口，无需改 TgtoDrive 源码：

    POST /api/visual-filter/config

请求体结构（场景 key 用 `scene` 字段区隔，光鸭为 `ENV_FILTER_GUANGYA`）：

    {
      "scene": "ENV_FILTER_GUANGYA",
      "rules": [ { "id": "...", "title": "...", "keywords": ["关键词"] } ],
      "parse_mode": 1,
      "all_mode": 0,
      "raw_regex": "",
      "advanced_raw_regex": ""
    }

三个必须知道的坑（已在本插件内处理）：
1. **全量覆盖，不是 append**：直接 POST 一条会把现有规则全冲掉。
   所以强制 `GET 当前 scene → 合并 → 整体 POST`。
2. **后端信 rules 不信 raw_regex**：绝不手拼 raw_regex，只维护 rules 数组，
   丢弃 GET 回来的陈旧 raw_regex，让后端按 rules 重新编译。
3. **页面"添加"按钮不发请求，只有"保存并回写"才发**：我们是后端直接 POST，
   不走页面按钮，所以无此坑；但注意单次 POST 即整体落盘。

写入的每条规则除 {id, title, keywords[]} 外，还会按配置带筛选字段：
分辨率(rule_resolution, 默认 4k) / 画面效果(rule_effect, 默认 all=全部效果) /
年份(取自 TMDB，自动填写) / 必包含关键词(rule_required_keyword, 默认 265=H.265/HEVC)。
字段名映射集中在模块常量 RULE_FIELD；TgtoDrive 的 OpenAPI 规范未导出 rules[]
内部字段结构，字段名系依据 UI 表单推断，若与实际不符请以「手动建一条规则后 GET
该 scene 配置」抓包为准，仅改 RULE_FIELD 一处即可。

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

# 待同步状态集合：下载中(1) / 手动接单(4) / 待入库(7)
# 注意：不含 0(待审批) —— 需求要求「审批后再触发」，未审批不占用 tg 频道监控。
PENDING_STATUS = (1, 4, 7)

# 审批通过后 EMBYPULSE 写入的状态码（management_router.batch_manage_action:
# action="approve" -> update_status(tid, sn, 1)）
STATUS_APPROVED = 1

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

# 自动入库检测最小间隔（秒）：避免每轮轮询都打 Emby 检索接口
INGEST_MIN_INTERVAL = 1800  # 30 分钟
# 本插件写入的监控规则 id 前缀：清理时只动这些，绝不波及用户其他关键词
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
    这样绝不会冲掉用户已在页面里配置的其他关键词。
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
    def _extract_rules(cfg):
        """从 GET 返回里稳妥取出 rules 数组。"""
        if not isinstance(cfg, dict):
            return []
        rules = cfg.get("rules")
        if isinstance(rules, list):
            return rules
        data = cfg.get("data")
        if isinstance(data, dict):
            r = data.get("rules")
            if isinstance(r, list):
                return r
        return []

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
            rule[RULE_FIELD["required"]] = req
        return rule

    # -------------------------------------------------------- 合并写（核心）
    def merge_and_save(self, items):
        """读-合并-写：把 items 追加进当前 scene 白名单，整体 POST 覆盖。

        返回 dict: {ok, added[], skipped[], changed, detail}
        - added  : 本次新写入的规则 id
        - skipped: 已存在（按 id 幂等）被跳过的规则 id
        - GET 或 POST 失败会抛 TgtoDriveError；调用方据此决定是否置工单完结。
        """
        # 坑①：先 GET 现有规则
        cfg = self.get_config()
        existing = self._extract_rules(cfg)
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

        merged = list(existing) + new_rules

        # 坑②：后端信 rules 不信 raw_regex，丢掉陈旧的 raw_regex 让其重新编译
        payload = dict(cfg) if isinstance(cfg, dict) else {}
        payload["scene"] = self.scene
        payload["rules"] = merged
        payload.pop("raw_regex", None)
        payload.pop("advanced_raw_regex", None)

        try:
            resp = requests.post(
                self._url(),
                json=payload,
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

    # -------------------------------------------------------- 删除（清理）
    def remove_rules(self, ids):
        """从当前 scene 白名单中删除指定 id 的规则（整体 POST 覆盖）。

        只删除传入的 id，绝不波及用户其他关键词；返回实际删除条数，无匹配则 0。
        """
        cfg = self.get_config()
        existing = self._extract_rules(cfg)
        id_set = set(ids or [])
        kept = [
            r for r in existing
            if not (isinstance(r, dict) and r.get("id") in id_set)
        ]
        removed_count = len(existing) - len(kept)
        if removed_count == 0:
            return 0
        payload = dict(cfg) if isinstance(cfg, dict) else {}
        payload["scene"] = self.scene
        payload["rules"] = kept
        # 坑②：后端信 rules 不信 raw_regex，丢弃陈旧 raw_regex 让其重新编译
        payload.pop("raw_regex", None)
        payload.pop("advanced_raw_regex", None)
        try:
            resp = requests.post(
                self._url(),
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise TgtoDriveError(f"POST 配置失败: {e}") from e
        if not (200 <= resp.status_code < 300):
            raise TgtoDriveError(
                f"删除规则 HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return removed_count

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
    description = "把审批通过的求片按 TMDB 编号写入 TgtoDrive 频道监控关键词白名单，并置工单完结"
    icon = "fa-tower-broadcast"
    icon_color = "from-sky-500 to-cyan-500"
    version = "1.2.0"
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
        self._last_ingest_result = None
        self._last_cleanup_result = None
        self._emby_uid_cache = (None, 0.0)
        self._setup_routes()

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
                "key": "check_interval",
                "label": "轮询间隔（分钟）",
                "type": "number",
                "placeholder": "5",
                "hint": "每隔多少分钟扫描一次待处理工单，默认 5",
            },
            {
                "key": "trigger_mode",
                "label": "触发时点",
                "type": "select",
                "options": [
                    {"value": "approved_only", "label": "仅审批通过后（推荐）— status=1"},
                    {"value": "approved_and_manual", "label": "审批通过 + 手动接单 — status=1,4"},
                    {"value": "all_pending", "label": "全部待同步 — status=1,4,7"},
                    {"value": "include_unapproved", "label": "包含未审批 — status=0,1,4,7（不推荐）"},
                ],
                "hint": "默认只处理审批通过后的工单（status=1）。未审批工单不会写入 tg 频道监控",
            },
            {
                "key": "mark_status",
                "label": "提交成功后的工单状态",
                "type": "select",
                "options": [
                    {"value": 2, "label": "已完成(2) — 直接完结"},
                    {"value": 7, "label": "待入库(7) — 等入库确认"},
                ],
                "hint": "默认完结为 2；选 7 则等实际入库后另做对账（注意 7 仍在待同步集合，会持续被跳过）",
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
            {
                "key": "auto_ingest_enabled",
                "label": "自动入库流转（特性2）",
                "type": "toggle",
                "hint": "开启后：待入库(7) 工单若 Emby 库已有该影视，自动流转为已入库(2)",
            },
            {
                "key": "ingest_interval",
                "label": "入库检测间隔（分钟）",
                "type": "number",
                "placeholder": "30",
                "hint": "每隔多少分钟扫描一次待入库工单并比对 Emby 库，默认 30",
            },
            {
                "key": "auto_cleanup_enabled",
                "label": "完结关键词清理（特性1）",
                "type": "toggle",
                "hint": "开启后：已入库且超过清理天数的工单，自动删除其 TgtoDrive 关键词监控",
            },
            {
                "key": "cleanup_days",
                "label": "清理期限（天）",
                "type": "number",
                "placeholder": "5",
                "hint": "工单完结后多少天删除对应关键词监控，默认 5；设 0 关闭清理",
            },
            {
                "key": "cleanup_interval",
                "label": "清理扫描间隔（分钟）",
                "type": "number",
                "placeholder": "60",
                "hint": "每隔多少分钟扫描一次待清理关键词，默认 60",
            },
            {
                "key": "cleanup_done_status",
                "label": "触发清理的完结状态",
                "type": "select",
                "options": [
                    {"value": 2, "label": "已完成/已入库(2)"},
                    {"value": 7, "label": "待入库(7)"},
                ],
                "hint": "达到该状态且超过清理天数即删除关键词；默认 2（已入库）",
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
        logger.info("[求片同步] 插件已启用，后台轮询启动")

    def on_disable(self):
        self._running = False
        self._stop_event.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2)
        if not t or not t.is_alive():
            self._thread = None
        logger.info("[求片同步] 插件已禁用")

    # ------------------------------------------------------------ 轮询主循环
    def _loop(self):
        # 启动后稍等，避免与 EMBYPULSE 自身初始化抢资源
        if self._stop_event.wait(30):
            return
        last_ingest = 0.0
        last_cleanup = 0.0
        while self._running and self._enabled:
            # 阶段A：审批通过 -> 写入 TgtoDrive 关键词监控
            try:
                self._sync_once()
            except Exception as e:  # 单轮异常不终止线程
                logger.error(f"[求片同步] 轮询异常: {e}", exc_info=True)
                self.log(f"轮询异常: {e}", "error")

            cfg = self._get_config()
            try:
                interval = max(1, int(cfg.get("check_interval") or 5)) * 60
            except (TypeError, ValueError):
                interval = 300
            try:
                ingest_interval = max(5, int(cfg.get("ingest_interval") or 30)) * 60
            except (TypeError, ValueError):
                ingest_interval = 1800
            try:
                cleanup_interval = max(10, int(cfg.get("cleanup_interval") or 60)) * 60
            except (TypeError, ValueError):
                cleanup_interval = 3600

            now = time.time()
            # 阶段B：待入库(7) 且 Emby 已有该影视 -> 自动流转为已入库(2)
            if now - last_ingest >= ingest_interval:
                try:
                    self._ingest_once()
                except Exception as e:
                    logger.error(f"[求片同步] 自动入库检测异常: {e}", exc_info=True)
                last_ingest = now
            # 阶段C：已入库(默认2) 且超过清理天数 -> 删除 TgtoDrive 关键词监控
            if now - last_cleanup >= cleanup_interval:
                try:
                    self._cleanup_once()
                except Exception as e:
                    logger.error(f"[求片同步] 关键词清理异常: {e}", exc_info=True)
                last_cleanup = now

            if self._stop_event.wait(interval):
                return

    # ------------------------------------------------------------ 同步策略
    @staticmethod
    def _allowed_status(trigger_mode):
        """按触发时点返回允许同步的状态集合。

        默认 approved_only：只处理审批通过后的工单（status=1）。
        """
        mode = (trigger_mode or "approved_only").strip()
        if mode == "approved_only":
            return (STATUS_APPROVED,)            # 仅审批通过（默认）
        if mode == "approved_and_manual":
            return (STATUS_APPROVED, 4)          # 审批通过 + 手动接单
        if mode == "all_pending":
            return PENDING_STATUS                # 待同步全集（不含未审批）
        if mode == "include_unapproved":
            return (0,) + PENDING_STATUS         # 含未审批（不推荐）
        return (STATUS_APPROVED,)

    # ------------------------------------------------------------ 单轮同步
    def _sync_once(self):
        cfg = self._get_config()
        base = _resolve_base_url(cfg)
        token = (cfg.get("tgtdrive_token") or "").strip()
        cookie = (cfg.get("tgtdrive_cookie") or "").strip()
        scene = (cfg.get("tgtdrive_scene") or DEFAULT_SCENE).strip()
        endpoint = (cfg.get("whitelist_endpoint") or VISUAL_FILTER_ENDPOINT).strip()
        dry_run = cfg.get("dry_run") in (True, "true", "1", 1)
        allowed = self._allowed_status(cfg.get("trigger_mode"))
        try:
            mark_status = int(cfg.get("mark_status") or 2)
        except (TypeError, ValueError):
            mark_status = 2

        if not base or not endpoint:
            msg = "TgtoDrive 地址或白名单接口未配置，跳过本轮"
            self._last_result = msg
            logger.info(f"[求片同步] {msg}")
            return

        # 规则级筛选字段（取值来自配置，字段名由 RULE_FIELD 映射）
        rule_fields = {
            "resolution": (cfg.get("rule_resolution") or "4k").strip(),
            "effect": (cfg.get("rule_effect") or "all").strip(),
            "required": (cfg.get("rule_required_keyword") or "").strip(),
        }

        client = TgtoDriveClient(base, token, endpoint, scene, cookie, logger=logger)

        rows = list_all_requests()
        candidates = []
        for r in rows:
            # 用列名取值，不用位置下标：DAO 返回 DataRow（带列名的 dict），
            # 位置下标会随 SELECT 列序变化而静默错位（实测列序已与注释版不同）。
            if _as_int(_row_get(r, "status")) not in allowed:
                continue
            candidates.append(r)

        if not candidates:
            self._last_result = "无待同步工单"
            return

        items = []
        for r in candidates:
            tmdb_id = _row_get(r, "tmdb_id")
            media_type = _row_get(r, "media_type")
            title = _row_get(r, "title")
            year = _row_get(r, "year")
            season = _row_get(r, "season")
            episodes = _row_get(r, "episodes") or ""
            if tmdb_id is None:
                continue
            keyword = self._build_keyword(title, media_type, year, season, episodes)
            if not keyword:
                continue
            items.append({
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "year": year,
                "season": season,
                "title": title,
                "status": _row_get(r, "status"),
                "keyword": keyword,
                "rule_fields": rule_fields,
            })

        if not items:
            self._last_result = "无可用关键词的待同步工单"
            return

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
            return

        # 读-合并-写
        try:
            result = client.merge_and_save(items)
        except TgtoDriveError as e:
            self._last_run = time.strftime("%Y-%m-%d %H:%M:%S")
            self._last_result = f"同步失败: {e}"
            self._stats["failed"] += len(items)
            logger.warning(f"[求片同步] {self._last_result}")
            self.log(f"同步失败: {e}", "warning", notify=True)
            return

        if not result["ok"]:
            self._last_run = time.strftime("%Y-%m-%d %H:%M:%S")
            self._last_result = f"同步失败: {result['detail']}"
            self._stats["failed"] += len(items)
            logger.warning(f"[求片同步] {self._last_result}")
            self.log(f"同步失败: {result['detail']}", "warning", notify=True)
            return

        # 写入成功：added + skipped 全部置完结（skipped=已存在=等效已监控）
        done = 0
        for it in items:
            # 避免对已经处于目标状态的工单重复写库
            if it["status"] == mark_status:
                done += 1
                continue
            try:
                update_media_request_status(it["tmdb_id"], it["season"], mark_status)
                done += 1
            except Exception as e:
                logger.error(
                    f"[求片同步] 白名单已写入但改状态失败 tmdb={it['tmdb_id']} s={it['season']}: {e}"
                )
                self.log(
                    f"白名单已写入但改状态失败: {it['title']} (TMDB {it['tmdb_id']}, S{it['season']}) — {e}",
                    "error",
                )

        added_n = len(result["added"])
        skipped_n = len(result["skipped"])
        self._last_run = time.strftime("%Y-%m-%d %H:%M:%S")
        self._last_result = (
            f"本轮待处理 {len(items)}，新增写入 {added_n}，已存在跳过 {skipped_n}，"
            f"置状态{mark_status}({STATUS_TEXT.get(mark_status,'')}) {done} 条"
        )
        self._stats["total"] += len(items)
        self._stats["success"] += added_n
        self._stats["skipped"] += skipped_n
        logger.info(f"[求片同步] {self._last_result}")
        if added_n:
            self.log(
                f"已同步 {added_n} 条至 TgtoDrive 白名单并置完结: "
                + ", ".join(it["keyword"] for it in items[:5]),
                "info",
            )

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

    # ------------------------------------------------------------ Emby 入库检测（特性2）
    @staticmethod
    def _norm_title(name):
        if not name:
            return ""
        return re.sub(r"[\s\-_.:，。！!?()（）\[\]【】]+", "", str(name)).lower()

    def _emby_admin_user_id(self):
        """取一个管理员 Emby 用户 id 用于库内检索，带 1 小时缓存。"""
        if media_api is None:
            return None
        uid, ts = self._emby_uid_cache
        if uid is not None and (time.time() - ts) < 3600:
            return uid
        try:
            resp = media_api.get("/Users", timeout=10)
            if resp.status_code != 200:
                return None
            users = resp.json() or []
            admin = next(
                (u for u in users if (u.get("Policy") or {}).get("IsAdministrator")),
                None,
            )
            chosen = admin or (users[0] if users else None)
            new_uid = chosen.get("Id") if chosen else None
        except Exception as e:
            logger.warning(f"[求片同步] 获取 Emby 用户失败: {e}")
            return None
        self._emby_uid_cache = (new_uid, time.time())
        return new_uid

    def _find_in_emby(self, tmdb_id, title, media_type, year):
        """在 Emby 媒体库检索是否已存在对应影视。命中返回 item id，否则 None。"""
        if media_api is None:
            return None
        if not media_api.health_check():
            return None
        user_id = self._emby_admin_user_id()
        if not user_id:
            return None
        item_types = "Series" if media_type == "tv" else "Movie"
        params = {
            "UserId": user_id,
            "Recursive": "true",
            "SearchTerm": title or "",
            "IncludeItemTypes": item_types,
            "Fields": "ProviderIds,ProductionYear",
            "Limit": 30,
        }
        try:
            resp = media_api.get("/Items", params=params, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception as e:
            logger.warning(f"[求片同步] Emby 检索异常 tmdb={tmdb_id}: {e}")
            return None
        items = (data or {}).get("Items") or []
        target = str(tmdb_id)
        norm_title = self._norm_title(title)
        for it in items:
            prov = it.get("ProviderIds") or {}
            if prov.get("Tmdb") == target:
                return it.get("Id")
            # 兜底：影视名（去噪）+ 年份 双重匹配
            if (
                year
                and it.get("ProductionYear")
                and str(it.get("ProductionYear")) == str(year)
                and norm_title
                and self._norm_title(it.get("Name")) == norm_title
            ):
                return it.get("Id")
        return None

    def _ingest_once(self):
        """特性2：待入库(7) 工单若 Emby 已有对应影视，自动流转为已入库(2)。"""
        cfg = self._get_config()
        if not (cfg.get("auto_ingest_enabled") in (True, "true", "1", 1)):
            return
        if media_api is None or not media_api.health_check():
            return
        dry_run = cfg.get("dry_run") in (True, "true", "1", 1)

        rows = list_all_requests()
        # 实测 status 为 int，但历史/JSON 往返可能是 "7"，用 _as_int 归一后比较
        pending = [r for r in rows if _as_int(_row_get(r, "status")) == 7]
        moved = 0
        for r in pending:
            tmdb_id = _row_get(r, "tmdb_id")
            media_type = _row_get(r, "media_type")
            title = _row_get(r, "title")
            year = _row_get(r, "year")
            season = _row_get(r, "season")
            if tmdb_id is None:
                continue
            found = self._find_in_emby(tmdb_id, title, media_type, year)
            if not found:
                continue
            if dry_run:
                logger.info(
                    f"[求片同步][试运行] 将自动流转为已入库: {title} (TMDB {tmdb_id}, S{season})"
                )
                continue
            try:
                update_media_request_status(tmdb_id, season, 2)
                moved += 1
                self.log(
                    f"检测到 Emby 已有《{title}》(TMDB {tmdb_id}, S{season})，自动流转为已入库",
                    "info",
                )
            except Exception as e:
                logger.error(
                    f"[求片同步] 自动入库流转失败 tmdb={tmdb_id} s={season}: {e}"
                )
        self._last_ingest_result = (
            f"本轮检测待入库 {len(pending)}，自动入库 {moved} 条"
            if not dry_run
            else "[试运行] 未实际改动"
        )
        if moved:
            logger.info(f"[求片同步] {self._last_ingest_result}")

    # ------------------------------------------------------------ 关键词清理（特性1）
    @staticmethod
    def _parse_ts(value):
        """解析 media_requests.updated_at（'YYYY-MM-DD HH:MM:SS'，UTC）为 epoch。"""
        if not value:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().replace("T", " ").replace("Z", "")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
        return None

    def _cleanup_once(self):
        """特性1：已完结(默认 status=2) 且超过清理天数(默认5) 的工单，删除其 TgtoDrive 关键词监控。

        实现前提：TgtoDrive 未暴露"频道监控完结"接口，本插件以 EMBYPULSE 工单"已入库/完结"
        状态作为代理信号（配合特性2，内容进 Emby 即视为监控完结）。如需更精确，可在 TgtoDrive
        暴露完结回调后接入。
        """
        cfg = self._get_config()
        if not (cfg.get("auto_cleanup_enabled") in (True, "true", "1", 1)):
            return
        try:
            cleanup_days = max(0, int(cfg.get("cleanup_days") or 5))
        except (TypeError, ValueError):
            cleanup_days = 5
        if cleanup_days <= 0:
            return
        try:
            done_status = int(cfg.get("cleanup_done_status") or 2)
        except (TypeError, ValueError):
            done_status = 2
        dry_run = cfg.get("dry_run") in (True, "true", "1", 1)

        base = _resolve_base_url(cfg)
        token = (cfg.get("tgtdrive_token") or "").strip()
        cookie = (cfg.get("tgtdrive_cookie") or "").strip()
        scene = (cfg.get("tgtdrive_scene") or DEFAULT_SCENE).strip()
        endpoint = (cfg.get("whitelist_endpoint") or VISUAL_FILTER_ENDPOINT).strip()
        if not base or not endpoint:
            return
        client = TgtoDriveClient(base, token, endpoint, scene, cookie, logger=logger)

        try:
            cfg_resp = client.get_config()
        except TgtoDriveError as e:
            self._last_cleanup_result = f"读取白名单失败: {e}"
            logger.warning(f"[求片同步] {self._last_cleanup_result}")
            return

        rules = client._extract_rules(cfg_resp)
        now = time.time()
        to_remove = []
        for rule in rules:
            rid = rule.get("id") if isinstance(rule, dict) else None
            if not rid or not rid.startswith(RULE_ID_PREFIX):
                continue
            m = re.match(r"^kw_(\d+)_(\d+)$", rid)
            if not m:
                continue
            tmdb_id = int(m.group(1))
            season = int(m.group(2))
            try:
                row = get_media_request(tmdb_id, season)
            except Exception:
                row = None
            if not row:
                # 工单已删除：保守起见不自动清理（避免误删仍需要的监控）
                continue
            # get_media_request 返回 DataRow（dict），按列名取并用 _as_int 归一，
            # 不按字符串判等（status 实测是 int，'' / "None" 之类只在 reject_reason 出现）
            if _as_int(_row_get(row, "status")) != done_status:
                continue
            ts = self._parse_ts(_row_get(row, "updated_at"))
            if ts is None:
                continue
            if now - ts < cleanup_days * 86400:
                continue
            to_remove.append(rid)

        if not to_remove:
            self._last_cleanup_result = "无超过清理期限的关键词"
            return

        if dry_run:
            self._last_cleanup_result = f"[试运行] 将清理 {len(to_remove)} 条: {to_remove}"
            logger.info(f"[求片同步] {self._last_cleanup_result}")
            return

        try:
            removed = client.remove_rules(to_remove)
        except TgtoDriveError as e:
            self._last_cleanup_result = f"清理失败: {e}"
            logger.warning(f"[求片同步] {self._last_cleanup_result}")
            self.log(f"关键词清理失败: {e}", "warning", notify=True)
            return

        self._last_cleanup_result = f"已清理 {removed} 条已完结关键词监控（>{cleanup_days}天）"
        logger.info(f"[求片同步] {self._last_cleanup_result}")
        self.log(self._last_cleanup_result, "info")

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
                    "trigger_mode": cfg.get("trigger_mode") or "approved_only",
                    "allowed_status": list(self._allowed_status(cfg.get("trigger_mode"))),
                    "dry_run": cfg.get("dry_run") in (True, "true", "1", 1),
                    "auto_ingest_enabled": cfg.get("auto_ingest_enabled") in (True, "true", "1", 1),
                    "auto_cleanup_enabled": cfg.get("auto_cleanup_enabled") in (True, "true", "1", 1),
                    "cleanup_days": cfg.get("cleanup_days") or 5,
                    "last_ingest_result": self._last_ingest_result,
                    "last_cleanup_result": self._last_cleanup_result,
                },
            }

        @self.router.post("/sync_now")
        async def api_sync_now(request: Request):
            """立即执行一次同步（管理员）"""
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

        @self.router.post("/ingest_now")
        async def api_ingest_now(request: Request):
            """立即执行一次自动入库检测（管理员）"""
            if not request.session.get("user"):
                return {"status": "error", "message": "未登录"}
            from app.domains.users import public_service as user_service

            if not user_service.is_admin_user(request):
                return {"status": "error", "message": "需要管理员权限"}
            try:
                self._ingest_once()
                return {
                    "status": "success",
                    "message": "已执行自动入库检测",
                    "data": {"result": self._last_ingest_result},
                }
            except Exception as e:
                logger.error(f"[求片同步] 手动入库检测失败: {e}", exc_info=True)
                return {"status": "error", "message": str(e)}

        @self.router.post("/cleanup_now")
        async def api_cleanup_now(request: Request):
            """立即执行一次关键词清理（管理员）"""
            if not request.session.get("user"):
                return {"status": "error", "message": "未登录"}
            from app.domains.users import public_service as user_service

            if not user_service.is_admin_user(request):
                return {"status": "error", "message": "需要管理员权限"}
            try:
                self._cleanup_once()
                return {
                    "status": "success",
                    "message": "已执行关键词清理",
                    "data": {"result": self._last_cleanup_result},
                }
            except Exception as e:
                logger.error(f"[求片同步] 手动清理失败: {e}", exc_info=True)
                return {"status": "error", "message": str(e)}

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
