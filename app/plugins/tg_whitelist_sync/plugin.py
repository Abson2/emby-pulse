"""
求片同步 TgtoDrive 关键词白名单插件（v2.1.0）
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

推送的每条规则按配置带筛选字段：
分辨率(rule_resolution, 默认 4k) / 画面效果(rule_effect, 默认 all=全部效果) /
年份(取自 TMDB，自动填写) / 必包含关键词(rule_required_keyword, 默认 265=H.265/HEVC)。

TgtoDrive 写入接口（v2.1.0：字段契约已按真实响应样本定稿）
---------------------------------------------------------------
依据资料库附件 `guangya_visual_filter.json`（GET /api/visual-filter/config 的真实
响应，2026-11）实证，不再是 UI 推断：

1. **GET 返回是「信封」**，顶层没有 rules：

       {
         "success": true,
         "scene": "ENV_FILTER_GUANGYA",           # 当前选中场景
         "current": { "key": "...", "rules": [...], "parse_mode": "meta", ... },
         "scenes": { "ENV_FILTER_GUANGYA": { "rules": [...] }, ... },
         "scene_labels": {...}, "tmdb_rule_cache": {...}, "image_base_url": "..."
       }

   `current` 是**当前选中场景**的快照，可能与插件配置的目标 scene 不同；
   因此定位规则时优先取 `scenes[目标scene].rules`，避免把规则串到别的场景。
   旧版本直接读顶层 `cfg["rules"]` 恒为空 → 只写新规则 → **覆盖**（bug1 根因）。

2. **rules[] 内字段名**（此前基于 UI 的推断全是错的，bug2 根因）：

       required_keyword        : "265"      # 必包含关键词（旧推断 must_include：后端不认）
       include_required_keyword: true       # 必含开关，不开则后端不编进正则
       effect                  : "all"      # 画面效果（旧推断 picture_effect：后端不认）
       resolution              : "4k"
       year                    : "2026"     # 字符串，不是 int
       include_year            : true
       rule_type               : "structured"
       source                  : "tmdb"
       tmdb_id                 : "297253"   # 字符串
       keyword_groups_any / aliases / media_name / preferred_name / title / keywords
       exclude_keywords / custom_regex / id / media_type / ...

   附件中插件写入的 `kw_297253_1` 经后端规范化后为
   `required_keyword:""` + `include_required_keyword:false` —— 正是「必包含关键词
   丢失」的实证：字段名错了、开关也没打开。

3. **后端信 rules 不信 raw_regex**：绝不手拼 raw_regex，只维护 rules 数组；
   丢弃 GET 回来的 `raw_regex` / `advanced_raw_regex` / `meta_raw_regex`，
   让后端按 rules 重新编译。

4. **POST 是整体覆盖，不是 append**：强制 `GET → 合并 → 整体 POST`，
   **完整保留非 kw_ 的用户规则**，只追加本插件新的 kw_ 规则。

写入体形态（配置项 `write_mode`）：
- `flat`（默认）：把 GET 里目标场景的配置对象提取成扁平体，补 `scene` 后 POST，
  与接口文档的 `{scene, rules, parse_mode, all_mode, ...}` 一致，不回传只读信封字段；
- `envelope`：原样回写整个 GET 信封（若现场后端要求完整信封，切到此项）。

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

# 光鸭白名单「结构化规则对象」字段名映射（v2.1.0 定稿）。
# 依据：资料库附件 guangya_visual_filter.json —— GET /api/visual-filter/config
# 的真实响应（2026-11）。此前基于 UI 推断的 must_include / picture_effect 均不存在，
# 后端会把未知字段丢弃，这正是「必包含关键词丢失」的根因。
# 取值（4k / all / 265）由插件配置项提供，字段名在此集中维护。
RULE_FIELD = {
    "resolution": "resolution",      # 分辨率（如 "4k" / "all"）
    "effect": "effect",              # 画面效果（"all" 表示全部效果）
    "year": "year",                  # 年份（字符串，如 "2026"）
    "required": "required_keyword",  # 必包含关键词（如 "265" / H.265）
}

# 与上面的取值字段配套的布尔开关：不开则后端不会把该条件编进 meta_raw_regex。
# 附件实证：include_required_keyword=false 时 required_keyword 为空串。
RULE_SWITCH = {
    "required": "include_required_keyword",
    "year": "include_year",
}

# 字段名仍可通过插件配置覆盖（万一 TgtoDrive 升级改名，无需改代码）。
# 但默认值已是附件实证的真名，1.6.0 用户若曾手填 must_include 需清空或改回。


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
                 cookie=None, timeout=15, logger=None, write_mode="flat"):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.endpoint = endpoint or VISUAL_FILTER_ENDPOINT
        self.scene = scene or DEFAULT_SCENE
        self.cookie = cookie or ""
        self.timeout = timeout
        self.write_mode = write_mode or "flat"
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
    def _find_scene_container(cfg, scene):
        """按目标 scene 精确定位配置对象（v2.1.0）。

        真实 GET 响应（附件实证）是信封：
            {"scene": "...", "current": {...rules...},
             "scenes": {"ENV_FILTER_GUANGYA": {...rules...}, ...}, ...}

        `current` 只是**当前选中场景**的快照，未必等于插件配置的目标 scene。
        若盲目取 current.rules，插件会把规则合并进别的场景 → 串场景 + 覆盖。
        优先级：scenes[目标scene] > current(仅当 key 匹配) > 顶层包含 rules 的对象。
        返回 (container_dict, 来源说明)；找不到返回 (None, None)。
        """
        if not isinstance(cfg, dict):
            return None, None
        scenes = cfg.get("scenes")
        if isinstance(scenes, dict):
            node = scenes.get(scene)
            if isinstance(node, dict) and isinstance(node.get("rules"), list):
                return node, "scenes[%s]" % scene
        cur = cfg.get("current")
        if isinstance(cur, dict) and isinstance(cur.get("rules"), list):
            key = cur.get("key")
            if not key or key == scene:
                return cur, "current"
        if isinstance(cfg.get("rules"), list):
            return cfg, "top-level"
        return None, None

    @staticmethod
    def _extract_rules(cfg, scene=None):
        """从 GET 返回里稳妥取出目标 scene 的 rules 数组。"""
        if scene:
            container, _ = TgtoDriveClient._find_scene_container(cfg, scene)
            if container is not None and isinstance(container.get("rules"), list):
                return container["rules"]
        container, subkey = TgtoDriveClient._find_rules_container(cfg)
        if container is not None:
            rules = container.get(subkey)
            if isinstance(rules, list):
                return rules
        return []

    @staticmethod
    def _strip_raw_regex(cfg):
        """递归删除 raw_regex / advanced_raw_regex / meta_raw_regex 键。

        后端信 rules 不信正则（附件里三者都是按 rules 编译出来的产物），
        留着陈旧值会让后端跳过重编译或写入过期条件。
        """
        if isinstance(cfg, dict):
            cfg.pop("raw_regex", None)
            cfg.pop("advanced_raw_regex", None)
            cfg.pop("meta_raw_regex", None)
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
        """把一条求片构造为 TgtoDrive「结构化」白名单规则对象。

        字段形态以资料库附件 guangya_visual_filter.json（GET 真实响应）为准：
        rule_type=structured、source=tmdb、tmdb_id/year 为字符串、
        keywords/aliases/keyword_groups_any 承载片名，配套 include_* 布尔开关。

        实测（附件中 kw_297253_1 即本插件写入后被后端规范化的规则）：后端会按
        rules 重新规范化并编译 meta_raw_regex，因此这里补齐结构化骨架，
        尤其必须写入 required_keyword **并打开 include_required_keyword**，
        否则后端会把必包含关键词规范化成空串（bug2）。
        """
        rid = self._rule_id(item["tmdb_id"], item["season"])
        title = (item.get("title") or "").strip() or f"TMDB{item['tmdb_id']}"
        keyword = item["keyword"]
        # 字段名可由插件配置覆盖（TgtoDrive 改名时无需改代码）；默认已是附件实证真名
        required_key = item.get("rule_required_field") or RULE_FIELD["required"]
        required_switch = item.get("rule_required_switch") or RULE_SWITCH["required"]
        year_key = RULE_FIELD["year"]
        year_switch = RULE_SWITCH["year"]

        rule = {
            "id": rid,
            "title": title,
            "media_name": title,
            "preferred_name": title,
            "aliases": [title],
            "keywords": [keyword],
            "keyword_groups_any": [[keyword]],
            "exclude_keywords": [],
            "custom_regex": "",
            "rule_type": "structured",
            "source": "tmdb",
            # 附件实证：tmdb_id / year 均为字符串，不是 int
            "tmdb_id": str(item["tmdb_id"]),
            "media_type": item.get("media_type") or "",
        }
        rf = item.get("rule_fields") or {}
        if rf.get("resolution"):
            rule[RULE_FIELD["resolution"]] = rf["resolution"]
        if rf.get("effect"):
            rule[RULE_FIELD["effect"]] = rf["effect"]
        year = item.get("year")
        if year:
            rule[year_key] = str(year)
            rule[year_switch] = True
        # 必包含关键词：值 + 开关缺一不可。留空时显式写空串/关闭开关，
        # 让后端规范化结果与附件一致（required_keyword:"" / include_*:false）
        req = (rf.get("required") or "").strip()
        rule[required_key] = req
        rule[required_switch] = bool(req)
        return rule

    # -------------------------------------------------------- 合并写（核心，永不删除）
    def merge_and_save(self, items):
        """读-合并-写：把 items 追加进目标 scene 白名单，整体 POST 覆盖。

        **关键保证**：只把本插件要写的新规则合并进去，**完整保留所有已存在的规则
        （含用户通过 TgtoDrive 页面手动添加的其它关键词）**，绝不删除任何用户规则。

        v2.1.0（依据附件 guangya_visual_filter.json 定稿）：
        - GET 返回是信封，rules 在 `current` / `scenes[scene]` 内、顶层没有 rules。
          旧代码读顶层 rules 恒为空 → 只写新规则 → 覆盖（bug1 根因）。
        - `current` 是「当前选中场景」，可能不是插件配置的目标场景，故优先按
          `scenes[目标scene]` 定位，避免串场景。
        - 写回时把合并后的规则同步到信封内所有副本（current / scenes[scene] /
          顶层 rules），后端无论读哪一份都不会拿到过期规则。

        安全护栏：GET 成功但结构完全无法解析、且不是「目标场景尚未创建」时，
        **中止推送**而非冒险覆盖现有规则。

        返回 dict: {ok, added[], skipped[], changed, detail}
        """
        cfg = self.get_config()
        scene = self.scene

        container, source = self._find_scene_container(cfg, scene)
        if container is None:
            # 区分三种情况：真·空配置 / 目标场景尚未创建 / 未知结构
            scenes = cfg.get("scenes") if isinstance(cfg, dict) else None
            scene_not_created = isinstance(scenes, dict) and scene not in scenes
            if cfg and not scene_not_created:
                raise TgtoDriveError(
                    "GET 返回无法定位目标场景的 rules（接口版本 / scene / 鉴权不匹配），"
                    "为安全起见中止推送，避免覆盖现有规则。请核对 tgtdrive_scene 配置，"
                    "或把 write_mode 切到 envelope 后重试。"
                )
            # 空配置(404 返回 {}) 或目标场景尚无配置：从零创建该场景
            if not isinstance(cfg, dict) or not cfg:
                cfg = {}
            if not isinstance(scenes, dict):
                cfg["scenes"] = {}
                scenes = cfg["scenes"]
            fresh = {"key": scene, "rules": []}
            scenes[scene] = fresh
            container, source = fresh, "scenes[%s](new)" % scene
            cfg.setdefault("current", fresh)
            cfg["scene"] = scene
        if self._log:
            self._log.info("[求片同步] 规则定位: %s，现有 %d 条",
                           source, len(container.get("rules") or []))

        existing = list(container.get("rules") or [])
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

        # 同步信封内的所有规则副本，避免后端读 current / scenes 拿到不同版本
        containers = [(container, "rules")]
        if isinstance(cfg, dict):
            cur = cfg.get("current")
            if isinstance(cur, dict) and cur is not container and isinstance(cur.get("rules"), list):
                if not cur.get("key") or cur.get("key") == scene:
                    containers.append((cur, "rules"))
            sc = cfg.get("scenes")
            node = sc.get(scene) if isinstance(sc, dict) else None
            if isinstance(node, dict) and node is not container and isinstance(node.get("rules"), list):
                containers.append((node, "rules"))
            if isinstance(cfg.get("rules"), list):
                containers.append((cfg, "rules"))
        for c, k in containers:
            c[k] = list(merged)

        # 坑②：后端信 rules 不信正则，递归丢弃陈旧正则让其重新编译
        self._strip_raw_regex(cfg)

        if (self.write_mode or "flat").strip().lower() == "envelope":
            # 原样回写整个 GET 信封（现场后端若要求完整信封，用此模式）
            cfg["scene"] = scene
            payload = cfg
        else:
            # 默认 flat：提取目标场景配置对象为扁平写入体，不回传 success /
            # scene_labels / tmdb_rule_cache 等只读信封字段
            payload = {
                k: v for k, v in container.items()
                if k not in ("rules", "updated_at", "success")
            }
            payload["scene"] = scene
            payload["rules"] = list(merged)
            self._strip_raw_regex(payload)

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
        rules = self._extract_rules(cfg, self.scene)
        return True, f"可达，场景 {self.scene} 当前 {len(rules)} 条规则", len(rules)


class TgWhitelistSyncPlugin(PluginBase):
    id = "tg_whitelist_sync"
    name = "求片同步 TgtoDrive 白名单"
    description = "仅推送：把审批通过/手动接单的求片按 TMDB 写入 TgtoDrive 频道监控白名单（分辨率4k/全部效果/年份TMDB/必含265）；字段名依真实响应定稿；清理交由 TgtoDrive 侧插件"
    icon = "fa-tower-broadcast"
    icon_color = "from-sky-500 to-cyan-500"
    version = "2.1.0"
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
                "label": "规则·必含字段名（高级，默认 required_keyword）",
                "type": "text",
                "placeholder": "required_keyword",
                "hint": "依据 TgtoDrive 真实响应样本（2026-11）定稿为 required_keyword。旧版本误用 must_include 导致必含关键词被后端丢弃；若已在旧版手填过 must_include，请清空或改回 required_keyword",
            },
            {
                "key": "rule_required_switch",
                "label": "规则·必含开关字段名（高级，默认 include_required_keyword）",
                "type": "text",
                "placeholder": "include_required_keyword",
                "hint": "必包含关键词的布尔开关；不打开后端不会把该条件编进正则。留空用默认 include_required_keyword",
            },
            {
                "key": "write_mode",
                "label": "写入体形态（高级，默认 flat）",
                "type": "select",
                "options": [
                    {"value": "flat", "label": "flat — 提取目标场景配置为扁平体（推荐）"},
                    {"value": "envelope", "label": "envelope — 原样回写整个 GET 信封"},
                ],
                "hint": "GET /api/visual-filter/config 返回的是含 current/scenes 的信封。默认只把目标场景配置提取成扁平体写回；若现场后端要求完整信封（POST 400/规则不生效），切到 envelope",
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
        # 字段名可配置，但旧版本(<=1.6.0)默认/手填的 must_include 已被附件实证为
        # 后端不认的错误名 —— 自动纠正，避免存量部署升级后仍丢必含关键词。
        raw_field = (cfg.get("rule_required_field") or "").strip()
        if raw_field in ("must_include", "picture_effect"):
            logger.warning(
                f"[求片同步] 检测到旧版错误字段名 {raw_field!r}，已自动纠正为 {RULE_FIELD['required']!r}"
            )
            raw_field = ""
        rule_required_field = raw_field or RULE_FIELD["required"]
        rule_required_switch = (cfg.get("rule_required_switch") or "").strip() or RULE_SWITCH["required"]
        write_mode = (cfg.get("write_mode") or "flat").strip() or "flat"
        if rule_fields["required"]:
            logger.info(
                f"[求片同步] 必包含关键词写入 {rule_required_field}={rule_fields['required']!r} "
                f"并置 {rule_required_switch}=True（字段名/开关依据 TgtoDrive 真实响应定稿）"
            )

        client = TgtoDriveClient(base, token, endpoint, scene, cookie,
                                 logger=logger, write_mode=write_mode)

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
                "rule_required_switch": rule_required_switch,
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
