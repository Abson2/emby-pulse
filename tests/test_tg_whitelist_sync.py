"""
求片同步 TgtoDrive 白名单插件 A（v2.2.0）—— 回归 / 生命周期测试

v2.1.0：字段契约按资料库附件 guangya_visual_filter.json（GET 真实响应）定稿：
必含字段名 required_keyword + 开关 include_required_keyword、效果 effect、
年份/tmdb_id 为字符串、GET 信封 rules 在 current / scenes[scene] 内。

v2.2.0：新增「删单联动移除」—— 工单删除事件下只移除 kw_<tmdb>_<season>，
完整保留用户自建规则与其它工单规则，并带全套安全护栏。插件 B 仍独立负责
「入库完结 → 周期清理」，两条删除路径互不替代。

不依赖真实 app / Emby / TgtoDrive，通过注入假模块与可控 transport 验证：
- 推送：审批后/手动接单后写入白名单，全量覆盖不冲掉现有规则（坑①），丢弃陈旧 raw_regex（坑②）
- 推送：合并写入绝不删除用户其它关键词；仅处理审批通过(1)/手动接单(4)，未审批(0) 不碰
- 幂等，重复运行不重复添加；事件驱动 push_items 推送成功后置 status=1
- 删单联动：remove_items 只删目标 kw_ 规则、保留用户规则与其它工单规则
- 删单联动护栏：GET 失败 / 空配置 / 结构未知 / 未命中 / dry_run 一律不 POST
- 全局 dry_run -> 任何阶段都不写库 / 不写 TgtoDrive
- 入库完结清理由插件 B 负责（本插件无 _cleanup_once / _ingest_once）
- build_rule 规则字段映射（分辨率/效果/年份/必包含）
"""

import sys
import types

# ----------------------------------------------------------------------------
# 1) 注入假依赖
# ----------------------------------------------------------------------------
def _make(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m

# --- fastapi 桩 ---
fastapi = _make("fastapi")
class _Request: pass
fastapi.Request = _Request
class _APIRouter:
    def __init__(self, *a, **k): pass
fastapi.APIRouter = _APIRouter
class _HTTPException(Exception): pass
fastapi.HTTPException = _HTTPException
def _Depends(*a, **k): pass
fastapi.Depends = _Depends

# --- 包骨架 ---
app = _make("app")
app_domains = _make("app.domains")
app_dm_media = _make("app.domains.media_requests")
app_plugins = _make("app.plugins")
app_infra = _make("app.infra")
app_infra_clients = _make("app.infra.clients")

# --- plugin_dao 桩 ---
plugin_dao = _make("app.plugins.plugin_dao")
plugin_dao.ensure_plugin_tables = lambda: None
plugin_dao.get_plugin_config = lambda pid: {}
plugin_dao.add_plugin_log = lambda *a, **k: None
plugin_dao.list_plugin_logs = lambda *a, **k: []
app_plugins.plugin_dao = plugin_dao

# --- base.PluginBase 桩 ---
base = _make("app.plugins.base")
class FakeRouter:
    def __init__(self, prefix): self.prefix = prefix; self.routes = []
    def get(self, path):
        def d(f): self.routes.append(("GET", path, f)); return f
        return d
    def post(self, path):
        def d(f): self.routes.append(("POST", path, f)); return f
        return d
class PluginBase:
    id = ""; name = ""; description = ""; icon = ""; icon_color = ""
    version = "0.0.0"; author = ""; pro_only = False; permissions = []
    def __init__(self):
        self.router = FakeRouter(f"/api/plugins/{self.id}")
        self._enabled = False
        self._config = {}
        self._init_logs_table()
        self._load_config_to_cache()
    def _init_logs_table(self): pass
    def _load_config_to_cache(self): pass
    def _get_config(self): return dict(self._config)
    def set_config(self, cfg): self._config = cfg
    @property
    def enabled(self): return self._enabled
    def enable(self): self._enabled = True; self.on_enable()
    def disable(self): self._enabled = False; self.on_disable()
    def on_enable(self): pass
    def on_disable(self): pass
    def log(self, message, level="info", notify=True): pass
    def get_meta(self): return {}
base.PluginBase = PluginBase

# --- media_request_dao 桩（函数体由测试动态替换；导入插件前先给默认桩）---
mrd = _make("app.domains.media_requests.media_request_dao")
mrd.list_all_requests = lambda: []
mrd.update_media_request_status = lambda *a, **k: None
mrd.get_media_request = lambda *a, **k: None
app_dm_media.media_request_dao = mrd

# --- media_server_client 桩（导入失败应被 try/except 吞掉，media_api=None）---
msc = _make("app.infra.clients.media_server_client")
class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code; self._json = json_data; self.text = text or ""
    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json
msc.media_api = None  # 即便能导入也置 None：插件 A v2.0.0 不再查 Emby
app_infra_clients.media_server_client = msc

# ----------------------------------------------------------------------------
# 2) 可控 requests transport
# ----------------------------------------------------------------------------
fake_requests = types.ModuleType("requests")
class _RequestException(Exception): pass
fake_requests.RequestException = _RequestException
sys.modules["requests"] = fake_requests

class _Resp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code; self._json = json_data; self.text = text or ""
    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json
class Transport:
    def __init__(self):
        self.calls = []
        self.get_handlers = {}
        self.post_handlers = {}
    def set_get(self, fragment, fn): self.get_handlers[fragment] = fn
    def set_post(self, fragment, fn): self.post_handlers[fragment] = fn
    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        for frag, fn in self.get_handlers.items():
            if frag in url:
                return fn(kw)
        return _Resp(200, {"rules": []})
    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        for frag, fn in self.post_handlers.items():
            if frag in url:
                return fn(kw)
        return _Resp(200, {})
transport = Transport()
fake_requests.get = transport.get
fake_requests.post = transport.post

# ----------------------------------------------------------------------------
# 3) 导入插件（文件方式，避免污染 repo 包）
# ----------------------------------------------------------------------------
import importlib.util
import os as _os
# 路径由测试文件位置推导，便于在 CI / 任意 checkout 目录直接运行
_REPO_ROOT = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
_PLUGIN_PATH = _os.path.join(_REPO_ROOT, "app", "plugins", "tg_whitelist_sync", "plugin.py")
SPEC = importlib.util.spec_from_file_location(
    "tg_whitelist_sync_test",
    _PLUGIN_PATH,
)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)
plugin = mod.plugin

# ----------------------------------------------------------------------------
# 4) 测试工具
# ----------------------------------------------------------------------------
PASS = 0
FAIL = 0
def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {msg}")
    else:
        FAIL += 1
        print(f"  ✗ {msg}")

class DataRow(dict):
    """与 app/infra/db/row.py 的 DataRow 等价的测试桩（dict 子类，键为列名）。"""
    def __init__(self, d):
        super().__init__(d)
        self._vals = list(d.values())

def make_row(tmdb, mtype, title, year, status, season=0, episodes=""):
    """构造与实测 list_all_requests() 完全同形的行（DataRow = dict 子类）。"""
    return DataRow({
        "tmdb_id": tmdb,
        "media_type": mtype,
        "title": title,
        "year": year,
        "poster_path": "https://image.tmdb.org/t/p/w500/abc.jpg",
        "status": status,
        "season": season,
        "created_at": "2026-11-05 05:35:05",
        "cnt": 1,
        "users": "Abson",
        "reject_reason": None,
        "episodes": episodes,
        "request_type": "new",
        "series_id": "",
    })

def reset_cfg(**over):
    cfg = {
        "tgtdrive_base_url": "http://tg",
        "tgtdrive_token": "tok",
        "tgtdrive_cookie": "",
        "tgtdrive_scene": "ENV_FILTER_GUANGYA",
        "whitelist_endpoint": "/api/visual-filter/config",
        "rule_resolution": "4k",
        "rule_effect": "all",
        "rule_required_keyword": "265",
        "check_interval": 5,
        "trigger_mode": "approved_and_manual",
        "dry_run": False,
        "notify_enabled": False,
    }
    cfg.update(over)
    plugin._config = cfg
    return cfg

update_calls = []
def record_update(*a, **k):
    update_calls.append(a)
# 插件用 `from ... import update_media_request_status` 绑进自身命名空间，须在 mod 上替换
mod.update_media_request_status = record_update

# 行注册表：让 list_all_requests 与 get_media_request 返回同一批行。
# 真实插件 _sync_once 用 list_all_requests 拿候选 (tmdb,season)，push_items 再
# 用 get_media_request 取完整行（标题/年份/状态），二者必须一致。
ROW_REGISTRY = {}
def _ik(x):
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return x
def set_rows(rows):
    ROW_REGISTRY.clear()
    for r in rows:
        ROW_REGISTRY[(_ik(r["tmdb_id"]), _ik(r.get("season", 0)))] = r
    mod.list_all_requests = lambda: list(rows)
    mod.get_media_request = lambda tmdb, season: ROW_REGISTRY.get((_ik(tmdb), _ik(season)))

# ----------------------------------------------------------------------------
# 5) 用例
# ----------------------------------------------------------------------------
print("\n== 阶段A：推送写入，全量覆盖不冲掉现有规则（坑①）==")
transport.calls.clear(); update_calls.clear()
reset_cfg()
# 状态置 4（手动接单中）：push_items 推送成功后才流转为 自动下载中(1)，便于校验置状态
set_rows([make_row(123, "movie", "Test Movie", 2024, 4, 0)])
existing_cfg = {"scene": "ENV_FILTER_GUANGYA",
                "rules": [{"id": "kw_999_0", "title": "Existing", "keywords": ["Other"]}],
                "raw_regex": "OLD_REGEX", "advanced_raw_regex": "OLD_ADV"}
transport.set_get("visual-filter", lambda kw: _Resp(200, existing_cfg))
captured = {}
def post_cfg(kw):
    captured["payload"] = kw.get("json")
    return _Resp(200, {})
transport.set_post("visual-filter", post_cfg)
plugin._sync_once()
rule_ids = [r["id"] for r in captured.get("payload", {}).get("rules", [])]
check("kw_999_0" in rule_ids, "现有规则 kw_999_0 被保留（未被冲掉）")
check("kw_123_0" in rule_ids, "新规则 kw_123_0 被追加")
check(captured.get("payload", {}).get("raw_regex") in (None, ""), "陈旧 raw_regex 已被丢弃（坑②）")
check(any(c[0]=="POST" for c in transport.calls), "已发起 POST 覆盖写")
check(update_calls == [(123, 0, 1)], f"工单推送成功置 自动下载中(1)：{update_calls}")

print("\n== 删除能力边界：仅删单联动，绝不误删用户规则 ==")
check(hasattr(plugin, "remove_items"), "提供 remove_items 事件驱动入口（删单联动）")
check(hasattr(mod.TgtoDriveClient, "remove_rules"), "client 提供 remove_rules 移除能力")
check(not hasattr(plugin, "remove_rules"),
      "Plugin 层不暴露裸 remove_rules（只走 remove_items 生成的 kw_ 白名单 id）")
check(not hasattr(plugin, "_cleanup_once"), "无 _cleanup_once（完结清理仍由插件 B 负责）")
check(not hasattr(plugin, "_ingest_once"), "无 _ingest_once（入库流转由核心负责）")

print("\n== 阶段A：未审批(0) 不触发 ==")
transport.calls.clear(); update_calls.clear()
reset_cfg()
mod.list_all_requests = lambda: [make_row(124, "movie", "Pending", 2024, 0, 0)]
transport.set_get("visual-filter", lambda kw: _Resp(200, {"rules": []}))
transport.set_post("visual-filter", lambda kw: _Resp(200, {}))
plugin._sync_once()
check(update_calls == [], "未审批工单未置状态")
check(not any(c[0]=="POST" for c in transport.calls), "未审批工单未写 TgtoDrive")

print("\n== 阶段A：幂等，重复运行不重复添加 ==")
transport.calls.clear(); update_calls.clear()
reset_cfg()
# 已存在 kw_125_2，且工单当前为手动接单(4)：应跳过写入（无 POST）但仍置状态为 自动下载中(1)
set_rows([make_row(125, "tv", "Series A", 2023, 4, 2)])
transport.set_get("visual-filter", lambda kw: _Resp(200, {"rules":[{"id":"kw_125_2","title":"Series A","keywords":["Series A S2"]}]}))
seen = {}
transport.set_post("visual-filter", lambda kw: (_ for _ in ()).throw(AssertionError("不应 POST")) if seen.setdefault("x",0) else _Resp(200,{}))
try:
    plugin._sync_once()
except AssertionError:
    check(False, "已存在规则时仍发起了 POST（不应）")
check(not any(c[0]=="POST" for c in transport.calls), "已存在规则 -> 无 POST（零净改动）")
check(update_calls == [(125, 2, 1)], f"已存在仍置 自动下载中：{update_calls}")

print("\n== 事件驱动：push_items 推送成功置 status=1（手动接单 4 → 1）==")
transport.calls.clear(); update_calls.clear()
reset_cfg()
mod.get_media_request = lambda tmdb, season: DataRow({
    "tmdb_id": tmdb, "media_type": "movie", "title": "Evt Movie", "year": 2024,
    "season": season, "episodes": "", "status": 4})
transport.set_get("visual-filter", lambda kw: _Resp(200, {"rules": []}))
transport.set_post("visual-filter", lambda kw: _Resp(200, {}))
res = plugin.push_items([(999, 0)])
check(res.get("ok") is True, f"push_items 返回 ok: {res}")
check(update_calls == [(999, 0, 1)], f"手动接单(4) 推送成功流转为 自动下载中(1)：{update_calls}")

print("\n== build_rule 规则字段映射（分辨率/效果/年份/必包含）==")
transport.calls.clear(); update_calls.clear()
reset_cfg(rule_resolution="4k", rule_effect="all", rule_required_keyword="265")
item = {"tmdb_id": 901, "season": 1, "title": "Field Test", "keyword": "Field Test S1",
        "media_type": "tv", "year": 2022, "rule_fields": {
            "resolution": "4k", "effect": "all", "required": "265"}}
r = mod.TgtoDriveClient("http://tg", "tok", "/api/visual-filter/config", "ENV_FILTER_GUANGYA").build_rule(item)
check(r.get("resolution") == "4k", f"分辨率字段已写入: {r.get('resolution')}")
check(r.get("effect") == "all", f"画面效果字段已写入(真实字段名 effect): {r.get('effect')}")
check(r.get("year") == "2022", f"年份(TMDB)已写入且为字符串: {r.get('year')}")
check(r.get("include_year") is True, "年份开关 include_year 已打开")
check(r.get("required_keyword") == "265", f"必包含关键词已写入(真实字段名 required_keyword): {r.get('required_keyword')}")
check(r.get("include_required_keyword") is True, "必含开关 include_required_keyword 已打开（否则后端丢弃必含条件）")
check("must_include" not in r and "picture_effect" not in r, "不再写入后端不认的旧推断字段名")
check(r["id"] == "kw_901_1", f"规则 id 幂等正确: {r['id']}")
check(r.get("rule_type") == "structured" and r.get("source") == "tmdb", "结构化规则骨架 rule_type/source 齐全")
check(r.get("tmdb_id") == "901", "tmdb_id 为字符串（与真实响应一致）")
item2 = dict(item); item2["rule_fields"] = {"resolution":"4k","effect":"all","required":""}
r2 = mod.TgtoDriveClient("http://tg","tok","/api/visual-filter/config","ENV_FILTER_GUANGYA").build_rule(item2)
check(r2.get("required_keyword") == "" and r2.get("include_required_keyword") is False,
      "必包含留空时显式写空串并关闭开关（与后端规范化结果一致）")

print("\n== 全局 dry_run：不写库 / 不写 TgtoDrive ==")
transport.calls.clear(); update_calls.clear()
reset_cfg(dry_run=True)
set_rows([
    make_row(777, "movie", "Dry Movie", 2024, 4, 0),
    make_row(778, "tv", "Dry Ingest", 2023, 1, 1),
])
posted_dry = []
transport.set_post("visual-filter", lambda kw: (posted_dry.append(kw.get("json")) or _Resp(200, {})))
plugin.push_items([(777, 0)])
plugin._sync_once()
check(update_calls == [], "dry_run 下未改动任何工单状态")
check(posted_dry == [], "dry_run 下未写 TgtoDrive")

print("\n== 真实数据回归（实测 DAO 返回，tmdb=1507696 天启Z：新世界）==")
transport.calls.clear(); update_calls.clear()
reset_cfg(rule_resolution="4k", rule_effect="all", rule_required_keyword="265")
# 工单当前为手动接单(4)：推送成功应流转为 自动下载中(1)，并校验中文标题/年份原样写入
set_rows([make_row(1507696, "movie", "天启Z：新世界", "2026", 4, 0)])
_store = {"rules": []}
transport.set_get("visual-filter", lambda kw: _Resp(200, {"rules": list(_store["rules"])}))
real_cap = {}
def _real_post(kw):
    real_cap["p"] = kw.get("json")
    _store["rules"] = list((kw.get("json") or {}).get("rules", []))
    return _Resp(200, {})
transport.set_post("visual-filter", _real_post)
plugin._sync_once()
rr = (real_cap.get("p") or {}).get("rules", [{}])[0]
check(update_calls == [(1507696, 0, 1)], f"int 型 status=1 推送成功置 自动下载中(1): {update_calls}")
check("image.tmdb.org/t/p/w500" not in str(rr.get("poster_path", "")), "未对 poster_path 二次拼接前缀")
check(rr.get("id") == "kw_1507696_0", f"movie season=0 生成幂等 id 正确: {rr.get('id')}")
check(rr.get("title") == "天启Z：新世界", f"中文标题原样保留: {rr.get('title')}")
check(rr.get("year") == "2026", f"年份取自 TMDB: {rr.get('year')}")
check(rr.get("resolution") == "4k" and rr.get("effect") == "all"
      and rr.get("required_keyword") == "265"
      and rr.get("include_required_keyword") is True,
      "4k / all / 265 + 必含开关四项规则字段均已按真实契约写入")
transport.calls.clear()
plugin._sync_once()
check(not any(c[0] == "POST" for c in transport.calls), "同一工单重复运行不重复写入（幂等）")

print("\n== preview / status 路由接线 ==")
prev = None; status_route = None
for meth, path, fn in plugin.router.routes:
    if meth == "GET" and path == "/preview":
        prev = fn
    if meth == "GET" and path == "/status":
        status_route = fn
check(prev is not None, "preview 路由已注册")
check(status_route is not None, "status 路由已注册")
check("/sync_now" in [p for _, p, _ in plugin.router.routes if _ == "POST" or True], "sync_now 路由已注册")
src = open(_PLUGIN_PATH, encoding="utf-8").read()
check('"request_count": _row_get(r, "cnt")' in src, "preview 用 cnt 映射 request_count（列表接口字段）")
check('"requested_by": _row_get(r, "users")' in src, "preview 用 users 映射 requested_by（列表接口字段）")
check('_row_get(r, "requested_by")' not in src, "未误用单查字段 requested_by 去读列表行")
# v2.0.0：不再有 ingest/cleanup 路由
check('"/ingest_now"' not in src, "已移除 ingest_now 路由")
check('"/cleanup_now"' not in src, "已移除 cleanup_now 路由")

# _resolve_base_url：多层解析优先级（地址解耦，免写死易变 IP）
import os as _os
import tempfile as _tf
rb = mod._resolve_base_url
check(rb({"tgtdrive_base_url": "http://ui:9999"}) == "http://ui:9999", "UI 显式地址优先")
_os.environ.pop("TGTODRIVE_URL", None)
_os.environ.pop("EMBYPULSE_CONFIG_DIR", None)
check(rb({}) == mod.DEFAULT_TGTODRIVE_URL, f"无配置时回落默认宿主网关: {mod.DEFAULT_TGTODRIVE_URL}")
_os.environ["TGTODRIVE_URL"] = "http://envhost:12366"
check(rb({}) == "http://envhost:12366", "环境变量 TGTODRIVE_URL 生效")
_tmpd = _tf.mkdtemp()
with open(_os.path.join(_tmpd, "tgtodrive.json"), "w", encoding="utf-8") as _f:
    _f.write('{"base_url": "http://filehost:12366"}')
_os.environ["EMBYPULSE_CONFIG_DIR"] = _tmpd
_os.environ.pop("TGTODRIVE_URL", None)
check(rb({}) == "http://filehost:12366", "本地配置文件 base_url 生效")
_os.environ.pop("EMBYPULSE_CONFIG_DIR", None)

# 运行态持久化：save/load 回读一致（跨重启后面板仍可读到历史，便于观测）
_d2 = _tf.mkdtemp()
_sp = _os.path.join(_d2, "tg_whitelist_sync_state.json")
_inst = mod.TgWhitelistSyncPlugin()
_inst._state_file = _sp
_inst._last_run = "2026-11-05 12:00:00"
_inst._last_result = "本轮待处理 2，新增写入 1，已存在跳过 1，置状态1 2 条"
_inst._stats = {"total": 2, "success": 1, "failed": 0, "skipped": 1}
_inst._save_state()
_inst2 = mod.TgWhitelistSyncPlugin()
_inst2._state_file = _sp
_inst2._load_state()
check(_inst2._last_run == "2026-11-05 12:00:00", "状态持久化：last_run 回读一致")
check(_inst2._last_result == "本轮待处理 2，新增写入 1，已存在跳过 1，置状态1 2 条", "状态持久化：last_result 回读一致")
check(_inst2._stats.get("total") == 2 and _inst2._stats.get("skipped") == 1, "状态持久化：stats 回读一致")

_rf = mod.TgWhitelistSyncPlugin()
_rf._state_file = None
_rfp = _rf._resolve_state_file()
check(isinstance(_rfp, str) and _rfp.endswith("tg_whitelist_sync_state.json"), "状态文件路径回落到配置目录")

print("\n== v2.1.0 真实信封回归（依据资料库附件 guangya_visual_filter.json）==")
# 附件结构：顶层无 rules；current 是「当前选中场景」(此处故意设为别的场景)，
# 目标场景规则只在 scenes[ENV_FILTER_GUANGYA].rules 里。
def _guangya_envelope():
    user_rule = {"id": "tmdb_movie_969681", "title": "蜘蛛侠：崭新之日", "keywords": []}
    kw_rule = {"id": "kw_297253_1", "title": "伟大的长征 S1", "keywords": ["4K"]}
    other_rule = {"id": "tmdb_movie_999999", "title": "别的场景规则", "keywords": []}
    return {
        "success": True,
        "scene": "ENV_FILTER_115",                 # 页面当前选中场景 != 插件目标场景
        "image_base_url": "https://image.tmdb.org/t/p",
        "scene_labels": {"ENV_FILTER_115": "115频道白名单",
                         "ENV_FILTER_GUANGYA": "光鸭频道白名单"},
        "current": {"key": "ENV_FILTER_115", "rules": [other_rule],
                    "parse_mode": "meta", "all_mode": False,
                    "raw_regex": "OLD", "meta_raw_regex": "OLD_META",
                    "advanced_raw_regex": "OLD_ADV"},
        "scenes": {
            "ENV_FILTER_115": {"key": "ENV_FILTER_115", "rules": [other_rule]},
            "ENV_FILTER_GUANGYA": {"key": "ENV_FILTER_GUANGYA",
                                   "label": "光鸭频道白名单",
                                   "rules": [user_rule, kw_rule],
                                   "parse_mode": "meta", "all_mode": False,
                                   "raw_regex": "OLD", "meta_raw_regex": "OLD_META"},
        },
        "tmdb_rule_cache": {"tv:297253": {}},
    }

transport.calls.clear(); update_calls.clear()
reset_cfg()
set_rows([make_row(555, "movie", "New Movie", "2026", 4, 0)])
env = _guangya_envelope()
transport.set_get("visual-filter", lambda kw: _Resp(200, env))
env_cap = {}
transport.set_post("visual-filter", lambda kw: (env_cap.update(p=kw.get("json")) or _Resp(200, {})))
plugin._sync_once()
p = env_cap.get("p") or {}
ids = [r.get("id") for r in p.get("rules", [])]
check("kw_297253_1" in ids and "tmdb_movie_969681" in ids,
      f"目标场景现有 2 条规则全部保留（未覆盖）: {ids}")
check("tmdb_movie_999999" not in ids, "未把 current/其它场景的规则串进目标场景")
check("kw_555_0" in ids, "新求片规则已增量追加到目标场景")
check(p.get("scene") == "ENV_FILTER_GUANGYA", "写入体 scene 指向插件配置的目标场景")
check("raw_regex" not in p and "meta_raw_regex" not in p and "advanced_raw_regex" not in p,
      "三类陈旧正则均已清除（后端按 rules 重编译）")
check("success" not in p and "scene_labels" not in p and "tmdb_rule_cache" not in p,
      "flat 模式不回传只读信封字段")
new_rule = next((r for r in p.get("rules", []) if r.get("id") == "kw_555_0"), {})
check(new_rule.get("required_keyword") == "265"
      and new_rule.get("include_required_keyword") is True,
      "真实信封下必含关键词与开关均正确写入")
check(new_rule.get("effect") == "all" and new_rule.get("year") == "2026",
      "真实信封下 effect / 年份字符串均正确")

print("\n== write_mode=envelope 与旧配置自动纠正 ==")
transport.calls.clear(); update_calls.clear()
reset_cfg(write_mode="envelope", rule_required_field="must_include")
set_rows([make_row(556, "movie", "Envelope Movie", "2026", 4, 0)])
env2 = _guangya_envelope()
transport.set_get("visual-filter", lambda kw: _Resp(200, env2))
env_cap2 = {}
transport.set_post("visual-filter", lambda kw: (env_cap2.update(p=kw.get("json")) or _Resp(200, {})))
plugin._sync_once()
p2 = env_cap2.get("p") or {}
check("scenes" in p2 and p2.get("scene") == "ENV_FILTER_GUANGYA",
      "envelope 模式原样回写整个 GET 信封")
check("raw_regex" not in p2.get("scenes", {}).get("ENV_FILTER_GUANGYA", {}),
      "envelope 模式下场景内陈旧正则也已清除")
rule556 = next((r for r in p2.get("scenes", {}).get("ENV_FILTER_GUANGYA", {}).get("rules", [])
                if r.get("id") == "kw_556_0"), {})
check(rule556.get("required_keyword") == "265" and "must_include" not in rule556,
      "旧版残留的错误字段名 must_include 被自动纠正为 required_keyword")
check(env2["scenes"]["ENV_FILTER_GUANGYA"]["rules"] is p2["scenes"]["ENV_FILTER_GUANGYA"]["rules"] or
      len(env2["scenes"]["ENV_FILTER_GUANGYA"]["rules"]) ==
      len(p2["scenes"]["ENV_FILTER_GUANGYA"]["rules"]),
      "信封内目标场景规则副本已同步")

print("\n== 目标场景尚未创建 / 未知结构护栏 ==")
transport.calls.clear(); update_calls.clear()
reset_cfg(tgtdrive_scene="ENV_FILTER_189")
set_rows([make_row(557, "movie", "Fresh Scene", "2026", 4, 0)])
transport.set_get("visual-filter", lambda kw: _Resp(200, {"success": True, "scenes": {}}))
fresh_cap = {}
transport.set_post("visual-filter", lambda kw: (fresh_cap.update(p=kw.get("json")) or _Resp(200, {})))
plugin._sync_once()
check(any(c[0] == "POST" for c in transport.calls), "目标场景无配置时允许从零创建并推送")
check(any(r.get("id") == "kw_557_0" for r in (fresh_cap.get("p") or {}).get("rules", [])),
      "新建场景的规则已写入")

transport.calls.clear(); update_calls.clear()
reset_cfg()
set_rows([make_row(558, "movie", "Guard", "2026", 4, 0)])
transport.set_get("visual-filter", lambda kw: _Resp(200, {"success": True, "weird": {"unknown": 1}}))
guard_cap = {}
transport.set_post("visual-filter", lambda kw: (guard_cap.update(p=kw.get("json")) or _Resp(200, {})))
plugin._sync_once()
check(not any(c[0] == "POST" for c in transport.calls),
      "GET 结构完全未知时护栏中止，绝不冒险覆盖")

print("\n== 1.8.0 删单联动：只删目标 kw_ 规则，完整保留其它规则 ==")
posted = []
transport.set_post("visual-filter", lambda kw: (posted.append(kw.get("json")) or _Resp(200, {})))

transport.calls.clear(); posted.clear(); update_calls.clear()
reset_cfg()
env = _guangya_envelope()
env["scenes"]["ENV_FILTER_GUANGYA"]["rules"].append(
    {"id": "kw_888_1", "title": "Other Request S1", "keywords": ["Other S1"]})
transport.set_get("visual-filter", lambda kw: _Resp(200, env))
res = plugin.remove_items([(297253, 1)])
p = posted[0] if posted else {}
ids = [r.get("id") for r in p.get("rules", [])]
check(res.get("ok") is True and res.get("changed") is True, f"remove_items 返回成功: {res}")
check("kw_297253_1" not in ids, "目标工单的 kw_297253_1 已移除")
check("tmdb_movie_969681" in ids, "用户自建 tmdb_movie_ 规则完整保留")
check("kw_888_1" in ids, "其它工单的 kw_ 规则完整保留（不误删）")
check(res.get("removed") == ["kw_297253_1"], f"仅报告移除目标 id: {res.get('removed')}")
check(res.get("kept_count") == 2, f"保留 2 条其它规则: {res.get('kept_count')}")
check(p.get("scene") == "ENV_FILTER_GUANGYA", "写入体 scene 指向插件配置的目标场景")
check("raw_regex" not in p and "meta_raw_regex" not in p and "advanced_raw_regex" not in p,
      "删单联动同样清除三类陈旧正则")
check("success" not in p and "scenes" not in p, "flat 模式不回传只读信封字段")
check(len(env["scenes"]["ENV_FILTER_GUANGYA"]["rules"]) == 2,
      "信封内目标场景规则副本已同步为 kept")
check(len(env["scenes"]["ENV_FILTER_115"]["rules"]) == 1
      and env["scenes"]["ENV_FILTER_115"]["rules"][0].get("id") == "tmdb_movie_999999",
      "其它场景 / current 所指场景的规则未被改动")
check(update_calls == [], "删单联动不改动任何工单状态")

print("\n== 删单联动护栏：未命中 / 空配置 / 结构未知 / GET 失败 / dry_run ==")
transport.calls.clear(); posted.clear()
reset_cfg()
transport.set_get("visual-filter", lambda kw: _Resp(200, _guangya_envelope()))
res = plugin.remove_items([(999999, 9)])
check(res.get("changed") is False and res.get("removed") == [],
      "未命中目标规则时返回零改动")
check(posted == [], "未命中时不 POST（零净改动）")

transport.calls.clear(); posted.clear()
transport.set_get("visual-filter", lambda kw: _Resp(404, {}))
res = plugin.remove_items([(297253, 1)])
check(res.get("ok") is False, "GET 404 空配置时中止并返回失败")
check(posted == [], "空配置时绝不 POST（避免清空现有规则）")

transport.calls.clear(); posted.clear()
transport.set_get("visual-filter", lambda kw: _Resp(200, {"success": True, "weird": {"unknown": 1}}))
res = plugin.remove_items([(297253, 1)])
check(res.get("ok") is False, "GET 结构未知时护栏中止")
check(posted == [], "结构未知时绝不 POST")

transport.calls.clear(); posted.clear()
transport.set_get("visual-filter", lambda kw: _Resp(500, {}, text="boom"))
res = plugin.remove_items([(297253, 1)])
check(res.get("ok") is False, "GET HTTP 500 时返回失败（异常被兜住，不冒到删除主流程）")
check(posted == [], "GET 失败时绝不 POST")

transport.calls.clear(); posted.clear()
reset_cfg(dry_run=True)
transport.set_get("visual-filter", lambda kw: _Resp(200, _guangya_envelope()))
res = plugin.remove_items([(297253, 1)])
check(res.get("changed") is False, "dry_run 返回零改动")
check(posted == [], "dry_run 不写 TgtoDrive")
check(not any(c[0] == "GET" for c in transport.calls), "dry_run 连 GET 都不发起")

print("\n== 删单联动：批量去重 / envelope 写入体 / 非 kw_ id 过滤 ==")
transport.calls.clear(); posted.clear()
reset_cfg()
env = _guangya_envelope()
env["scenes"]["ENV_FILTER_GUANGYA"]["rules"] = [
    {"id": "kw_111_0", "title": "A", "keywords": []},
    {"id": "tmdb_movie_222", "title": "User", "keywords": []},
    {"id": "kw_333_2", "title": "B", "keywords": []},
]
transport.set_get("visual-filter", lambda kw: _Resp(200, env))
res = plugin.remove_items([(111, 0), (333, 2), (111, 0)])
check(sorted(res.get("removed") or []) == ["kw_111_0", "kw_333_2"],
      "批量删除命中 2 条且重复候选已去重")
check([r.get("id") for r in (posted[0] if posted else {}).get("rules", [])] == ["tmdb_movie_222"],
      "批量删除后只剩用户自建规则")

transport.calls.clear(); posted.clear()
reset_cfg(write_mode="envelope")
transport.set_get("visual-filter", lambda kw: _Resp(200, _guangya_envelope()))
res = plugin.remove_items([(297253, 1)])
pe = posted[0] if posted else {}
ids_e = [r.get("id") for r in pe.get("scenes", {}).get("ENV_FILTER_GUANGYA", {}).get("rules", [])]
check("scenes" in pe and pe.get("scene") == "ENV_FILTER_GUANGYA",
      "envelope 模式原样回写整个 GET 信封")
check("kw_297253_1" not in ids_e and "tmdb_movie_969681" in ids_e,
      "envelope 模式下目标场景副本已移除目标规则且保留用户规则")
check("raw_regex" not in pe.get("scenes", {}).get("ENV_FILTER_GUANGYA", {}),
      "envelope 模式下陈旧正则也已清除")

transport.calls.clear(); posted.clear()
reset_cfg()
transport.set_get("visual-filter", lambda kw: _Resp(200, _guangya_envelope()))
cl = mod.TgtoDriveClient("http://tg", "tok", "/api/visual-filter/config", "ENV_FILTER_GUANGYA")
res2 = cl.remove_rules(["tmdb_movie_969681", "some_user_rule"])
check(res2.get("changed") is False and res2.get("removed") == [],
      "client 层过滤掉所有非 kw_ id，绝不误删用户规则")
check(posted == [], "非 kw_ id 请求不产生 POST")

print("\n== 删除入口接线（management_router.py） ==")
_mr_path = _os.path.join(_REPO_ROOT, "app", "domains", "media_requests", "management_router.py")
mrouter = open(_mr_path, encoding="utf-8").read()
check("_trigger_tg_whitelist_remove" in mrouter, "新增删单联动触发函数")
check("remove_candidates.append((tid, sn))" in mrouter,
      "delete 分支直接收集删除请求自带的 (tmdb_id, season)")
check('data.action == "delete" and remove_candidates' in mrouter,
      "仅 delete 动作触发联动（approve/manual/reject 不误触发）")
check("plugin.remove_items(candidates)" in mrouter, "异步守护线程调用 remove_items")
check('logger = logging.getLogger("uvicorn")' in mrouter,
      "模块级 logger 已定义（修复既有 push 钩子异常分支的 NameError）")

print(f"\n==== 结果：通过 {PASS} / 失败 {FAIL} ====")
sys.exit(1 if FAIL else 0)
