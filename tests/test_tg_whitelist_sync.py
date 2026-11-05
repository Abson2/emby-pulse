"""
求片同步 TgtoDrive 白名单插件 —— 回归 / 生命周期测试（v1.3.0）

不依赖真实 app / Emby / TgtoDrive，通过注入假模块与可控 transport 验证：
- 阶段A：审批后写入白名单，全量覆盖不冲掉现有规则（坑①），丢弃陈旧 raw_regex（坑②）
- 阶段A：仅处理审批通过(status=1)，未审批(0) 不碰
- 阶段A：幂等，重复运行不重复添加
- 阶段B（特性2）：待入库(7) 且 Emby 已有该影视 -> 自动流转 已入库(2)
- 阶段B：Emby 未命中 -> 不变更
- 阶段C（特性1）：已入库(2) 且超期 -> 删除对应 kw_ 规则，不影响用户其他规则
- 阶段C：未超期 -> 不删除
- 全局 dry_run -> 任何阶段都不写库 / 不写 TgtoDrive
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
plugin_dao.clear_plugin_logs = lambda *a, **k: True
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

# --- media_server_client 桩 ---
msc = _make("app.infra.clients.media_server_client")
class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code; self._json = json_data; self.text = text or ""
    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json
class FakeMediaApi:
    def __init__(self):
        self.health = True; self.api_key = "k"; self.host = "http://emby"
        self.users = [{"Id": "u1", "Policy": {"IsAdministrator": True}}]
        self.items = {}
        self.calls = []
    def health_check(self, timeout=3.0): return self.health
    def get(self, path, **kw):
        self.calls.append((path, kw))
        if path.endswith("/Users"):
            return _FakeResp(200, self.users)
        if path.endswith("/Items"):
            term = (kw.get("params") or {}).get("SearchTerm", "")
            return _FakeResp(200, {"Items": self.items.get(term, self.items.get("", []))})
        return _FakeResp(404, {})
msc.media_api = FakeMediaApi()

# ----------------------------------------------------------------------------
# 2) 可控 requests transport
# ----------------------------------------------------------------------------
# 不依赖真实 requests 包，直接造一个假 requests 模块
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
SPEC = importlib.util.spec_from_file_location(
    "tg_whitelist_sync_test",
    "/tmp/ep_repo/app/plugins/tg_whitelist_sync/plugin.py",
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
    """构造与实测 list_all_requests() 完全同形的行（DataRow = dict 子类）。

    实测字段：tmdb_id/media_type/title/year/poster_path/status/season/created_at/
    cnt/users/reject_reason/episodes/request_type/series_id
    注意 status 为 int、poster_path 为完整 URL、reject_reason 为 None。
    """
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
        "check_interval": 5,
        "trigger_mode": "approved_only",
        "mark_status": 2,
        "dry_run": False,
        "notify_enabled": False,
        "auto_ingest_enabled": False,
        "ingest_interval": 30,
        "auto_cleanup_enabled": False,
        "cleanup_days": 5,
        "cleanup_interval": 60,
        "cleanup_done_status": 2,
    }
    cfg.update(over)
    plugin._config = cfg
    return cfg

update_calls = []
def record_update(*a, **k):
    update_calls.append(a)
# 注意：插件用 `from ... import list_all_requests` 把名字绑进自身命名空间，
# 因此必须在 mod（插件模块）上替换，而不是在 mrd（DAO 模块）上。
mod.update_media_request_status = record_update

# ----------------------------------------------------------------------------
# 5) 用例
# ----------------------------------------------------------------------------
print("\n== 阶段A：审批后写入，全量覆盖不冲掉现有规则 ==")
transport.calls.clear(); update_calls.clear()
reset_cfg()
mod.list_all_requests = lambda: [make_row(123, "movie", "Test Movie", 2024, 1, 0)]
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
check(("POST", "http://tg/api/visual-filter/config",) and any(c[0]=="POST" for c in transport.calls), "已发起 POST 覆盖写")
check(update_calls == [(123, 0, 2)], f"工单置完结(2)：{update_calls}")

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
mod.list_all_requests = lambda: [make_row(125, "tv", "Series A", 2023, 1, 2)]
# GET 已包含该规则（说明上一轮已写入）
transport.set_get("visual-filter", lambda kw: _Resp(200, {"rules":[{"id":"kw_125_2","title":"Series A","keywords":["Series A S2"]}]}))
seen = {}
transport.set_post("visual-filter", lambda kw: (_ for _ in ()).throw(AssertionError("不应 POST")) if seen.setdefault("x",0) else _Resp(200,{}))
# 由于已存在，merge 不应产生新规则，POST 也不应发生
try:
    plugin._sync_once()
except AssertionError:
    check(False, "已存在规则时仍发起了 POST（不应）")
# 验证没有 POST 调用
check(not any(c[0]=="POST" for c in transport.calls), "已存在规则 -> 无 POST（零净改动）")
check(update_calls == [(125, 2, 2)], f"已存在仍置完结：{update_calls}")

print("\n== 阶段B（特性2）：待入库(7) 且 Emby 已有 -> 自动入库(2) ==")
transport.calls.clear(); update_calls.clear()
reset_cfg(auto_ingest_enabled=True)
mod.list_all_requests = lambda: [make_row(555, "tv", "My Series", 2023, 7, 1)]
msc.media_api.items = {"My Series": [{"Id": "i1", "Name": "My Series", "ProductionYear": 2023, "ProviderIds": {"Tmdb": "555"}}]}
plugin._ingest_once()
check(update_calls == [(555, 1, 2)], f"自动流转为已入库(2)：{update_calls}")

print("\n== 阶段B（特性2）：Emby 未命中 -> 不变更 ==")
transport.calls.clear(); update_calls.clear()
reset_cfg(auto_ingest_enabled=True)
mod.list_all_requests = lambda: [make_row(556, "tv", "Missing Show", 2023, 7, 1)]
msc.media_api.items = {}
plugin._ingest_once()
check(update_calls == [], "Emby 无匹配 -> 工单不变更")

print("\n== 阶段C（特性1）：已入库(2) 且超期 -> 删除对应 kw_ 规则 ==")
transport.calls.clear(); update_calls.clear()
reset_cfg(auto_cleanup_enabled=True, cleanup_days=5, cleanup_done_status=2)
transport.set_get("visual-filter", lambda kw: _Resp(200, {"rules":[
    {"id":"kw_123_0","title":"X","keywords":["x"]},
    {"id":"user_rule","title":"keep","keywords":["k"]},
]}))
mod.get_media_request = lambda tmdb, season: {"status":2, "updated_at":"2020-01-01 00:00:00"} if (tmdb,season)==(123,0) else None
cap2 = {}
transport.set_post("visual-filter", lambda kw: (cap2.update(payload=kw.get("json")) or _Resp(200, {})))
plugin._cleanup_once()
remaining = [r["id"] for r in cap2.get("payload", {}).get("rules", [])]
check("kw_123_0" not in remaining, "超期 kw_123_0 已被删除")
check("user_rule" in remaining, "用户规则 user_rule 被保留")
check(update_calls == [], "清理阶段不改动工单状态")

print("\n== 阶段C（特性1）：未超期 -> 不删除 ==")
transport.calls.clear(); update_calls.clear()
reset_cfg(auto_cleanup_enabled=True, cleanup_days=5, cleanup_done_status=2)
transport.set_get("visual-filter", lambda kw: _Resp(200, {"rules":[{"id":"kw_123_0","title":"X","keywords":["x"]}]}))
# 最近才完结（今天）
from datetime import datetime, timezone
recent = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
mod.get_media_request = lambda tmdb, season: {"status":2, "updated_at":recent}
posted = []
transport.set_post("visual-filter", lambda kw: (posted.append(kw.get("json")) or _Resp(200, {})))
plugin._cleanup_once()
check(posted == [], "未超期 -> 未发起删除 POST")

print("\n== 全局 dry_run：任何阶段都不写库 / 不写 TgtoDrive ==")
transport.calls.clear(); update_calls.clear()
reset_cfg(dry_run=True, auto_ingest_enabled=True, auto_cleanup_enabled=True)
mod.list_all_requests = lambda: [make_row(777, "movie", "Dry Run", 2024, 1, 0), make_row(778, "tv", "Dry Ingest", 2023, 7, 1)]
transport.set_get("visual-filter", lambda kw: _Resp(200, {"rules":[{"id":"kw_777_0","title":"Dry","keywords":["d"]}]}))
mod.get_media_request = lambda tmdb, season: {"status":2, "updated_at":"2020-01-01 00:00:00"} if (tmdb,season)==(777,0) else None
msc.media_api.items = {}
posted_dry = []
transport.set_post("visual-filter", lambda kw: (posted_dry.append(kw.get("json")) or _Resp(200, {})))
plugin._sync_once()
plugin._ingest_once()
plugin._cleanup_once()
check(update_calls == [], "dry_run 下未改动任何工单状态")
check(posted_dry == [], "dry_run 下未写 TgtoDrive")

print("\n== build_rule 规则字段映射（分辨率/效果/年份/必包含）==")
transport.calls.clear(); update_calls.clear()
reset_cfg(rule_resolution="4k", rule_effect="all", rule_required_keyword="265")
item = {"tmdb_id": 901, "season": 1, "title": "Field Test", "keyword": "Field Test S1",
        "media_type": "tv", "year": 2022, "rule_fields": {
            "resolution": "4k", "effect": "all", "required": "265"}}
r = mod.TgtoDriveClient("http://tg", "tok", "/api/visual-filter/config", "ENV_FILTER_GUANGYA").build_rule(item)
check(r.get("resolution") == "4k", f"分辨率字段已写入: {r.get('resolution')}")
check(r.get("picture_effect") == "all", f"画面效果字段已写入(映射名 picture_effect): {r.get('picture_effect')}")
check(r.get("year") == 2022, f"年份(TMDB)已写入: {r.get('year')}")
check(r.get("must_include") == "265", f"必包含关键词已写入(映射名 must_include): {r.get('must_include')}")
check(r["id"] == "kw_901_1", f"规则 id 幂等正确: {r['id']}")
# 必包含留空 -> 不应出现该字段
item2 = dict(item); item2["rule_fields"] = {"resolution":"4k","effect":"all","required":""}
r2 = mod.TgtoDriveClient("http://tg","tok","/api/visual-filter/config","ENV_FILTER_GUANGYA").build_rule(item2)
check("must_include" not in r2, "必包含留空时不写入该字段")

# ----------------------------------------------------------------------------
# 真实数据回归：用容器内 DAO 实测返回（tmdb 1507696《天启Z：新世界》）校准
# ----------------------------------------------------------------------------
print("\n== 真实数据回归（实测 DAO 返回，tmdb=1507696 天启Z：新世界）==")
transport.calls.clear(); update_calls.clear()
reset_cfg(rule_resolution="4k", rule_effect="all", rule_required_keyword="265")
mod.list_all_requests = lambda: [make_row(1507696, "movie", "天启Z：新世界", "2026", 1, 0)]
# 模拟真实落盘：GET 返回当前已保存的规则（首次为空，POST 后应能读到）
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

# 坑2：status 为 int（这里断言取证链路：int 1 被正确识别为“审批通过”）
check(update_calls == [(1507696, 0, 2)], f"int 型 status=1 被正确识别并置完结: {update_calls}")
# 坑1：poster_path 是完整 URL，插件从不在其上拼前缀（不应出现在规则里，也不应被改写）
check("image.tmdb.org/t/p/w500" not in str(rr.get("poster_path", "")), "未对 poster_path 二次拼接前缀")
# 坑4：movie 的 season=0 / episodes="" 恒定存在，不应因 movie 而丢字段或报错
check(rr.get("id") == "kw_1507696_0", f"movie season=0 生成幂等 id 正确: {rr.get('id')}")
# 坑3：reject_reason 为 None，不影响流程（走到这里即说明未因判空炸掉）
check(rr.get("title") == "天启Z：新世界", f"中文标题原样保留: {rr.get('title')}")
# 规则字段：year 取自 TMDB（字符串 "2026" 原样写入）
check(rr.get("year") == "2026", f"年份取自 TMDB: {rr.get('year')}")
check(rr.get("resolution") == "4k" and rr.get("picture_effect") == "all"
      and rr.get("must_include") == "265", "4k / all / 265 三项规则字段均已写入")
# 幂等：同一工单再跑一次不应重复 POST
transport.calls.clear()
plugin._sync_once()
check(not any(c[0] == "POST" for c in transport.calls), "同一工单重复运行不重复写入（幂等）")

# 坑5：list_all_requests 用 cnt/users，单查用 request_count/requested_by ——
# preview 路由应把 cnt/users 映射为 request_count/requested_by（不混用两套字段）
prev = None
for meth, path, fn in plugin.router.routes:
    if meth == "GET" and path == "/preview":
        prev = fn
check(prev is not None, "preview 路由已注册")
src = open("/tmp/ep_repo/app/plugins/tg_whitelist_sync/plugin.py", encoding="utf-8").read()
check('"request_count": _row_get(r, "cnt")' in src, "preview 用 cnt 映射 request_count（列表接口字段）")
check('"requested_by": _row_get(r, "users")' in src, "preview 用 users 映射 requested_by（列表接口字段）")
check('_row_get(r, "requested_by")' not in src, "未误用单查字段 requested_by 去读列表行")

# _resolve_base_url：多层解析优先级（地址解耦，免写死易变 IP）
import os as _os
import tempfile as _tf
rb = mod._resolve_base_url
# 1) UI 显式配置最高优先
check(rb({"tgtdrive_base_url": "http://ui:9999"}) == "http://ui:9999",
      "UI 显式地址优先")
# 2) UI 为空且无 env / 无文件 -> 默认宿主网关 172.17.0.1
_os.environ.pop("TGTODRIVE_URL", None)
_os.environ.pop("EMBYPULSE_CONFIG_DIR", None)
check(rb({}) == mod.DEFAULT_TGTODRIVE_URL,
      f"无配置时回落默认宿主网关: {mod.DEFAULT_TGTODRIVE_URL}")
# 3) 环境变量 TGTODRIVE_URL 次之
_os.environ["TGTODRIVE_URL"] = "http://envhost:12366"
check(rb({}) == "http://envhost:12366", "环境变量 TGTODRIVE_URL 生效")
# 4) 本地配置文件 <dir>/tgtodrive.json 再次之
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
_inst._last_result = "本轮待处理 2，新增写入 1，已存在跳过 1，置状态2 2 条"
_inst._last_ingest_run = "2026-11-05 12:05:00"
_inst._last_cleanup_run = "2026-11-05 12:10:00"
_inst._stats = {"total": 2, "success": 1, "failed": 0, "skipped": 1}
_inst._save_state()
_inst2 = mod.TgWhitelistSyncPlugin()
_inst2._state_file = _sp
_inst2._load_state()
check(_inst2._last_run == "2026-11-05 12:00:00", "状态持久化：last_run 回读一致")
check(_inst2._last_result == "本轮待处理 2，新增写入 1，已存在跳过 1，置状态2 2 条", "状态持久化：last_result 回读一致")
check(_inst2._last_ingest_run == "2026-11-05 12:05:00", "状态持久化：last_ingest_run 回读一致")
check(_inst2._last_cleanup_run == "2026-11-05 12:10:00", "状态持久化：last_cleanup_run 回读一致")
check(_inst2._stats.get("total") == 2 and _inst2._stats.get("skipped") == 1, "状态持久化：stats 回读一致")

# 未显式指定时，状态文件路径回落到配置目录（不报错、命名正确）
_rf = mod.TgWhitelistSyncPlugin()
_rf._state_file = None
_rfp = _rf._resolve_state_file()
check(isinstance(_rfp, str) and _rfp.endswith("tg_whitelist_sync_state.json"), "状态文件路径回落到配置目录")

# /status 接口接线了 ingest/cleanup 运行时间戳字段（源码级校验）
check('"last_ingest_run": self._last_ingest_run' in src, "status 接口返回 last_ingest_run")
check('"last_cleanup_run": self._last_cleanup_run' in src, "status 接口返回 last_cleanup_run")

print(f"\n==== 结果：通过 {PASS} / 失败 {FAIL} ====")
sys.exit(1 if FAIL else 0)
