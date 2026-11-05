"""
求片完结关键词清理（TgtoDrive 插件 B，v1.0.0）—— 单元 / 集成测试

只依赖 Python 标准库；通过替换模块内的 _http_json / _emby_has_item / time.time
验证：
- _extract_rules 兼容 {rules} 与 {data:{rules}} 两种返回
- _emby_has_item 的精确匹配 / 名称年份兜底 / 不可达保守返回 None 三条分支
- run()：清理权严格限定 kw_ 前缀（绝不碰用户规则）
- run()：入库满 cleanup_days 才删；未到期不删；未入库清计时；Emby 不可达保守不删
- run()：删除采用读-过滤-整体 POST 覆盖，丢弃 raw_regex
- run()：stats.json 持久化计时；cleanup_days<=0 跳过
"""

import sys
import json
import types
import importlib.util
import tempfile
import os

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ " + msg)
    else:
        FAIL += 1
        print("  ✗ " + msg)


# ---------------------------------------------------------------------------
# 导入插件模块（文件方式，避免污染 repo 包）
# ---------------------------------------------------------------------------
SPEC = importlib.util.spec_from_file_location(
    "tgtdrive_monitor_test",
    "/tmp/ep_repo/tgtdrive-monitor/plugin.py",
)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


# ---------------------------------------------------------------------------
# 工具：构造白名单规则
# ---------------------------------------------------------------------------
def kw_rule(tmdb, season, title="T", media_type="movie", year="2024"):
    return {"id": "kw_%d_%d" % (tmdb, season), "title": title,
            "media_type": media_type, "year": year, "keywords": [title]}


def user_rule(rid="user_rule_1", title="My Manual"):
    return {"id": rid, "title": title, "keywords": [title]}


# ---------------------------------------------------------------------------
# 1) _extract_rules / _urlencode 单元
# ---------------------------------------------------------------------------
print("\n== _extract_rules / _urlencode ==")
check(mod._extract_rules({"rules": [1, 2]}) == [1, 2], "直取 rules 列表")
check(mod._extract_rules({"data": {"rules": [3]}}) == [3], "兼容 data.rules 嵌套")
check(mod._extract_rules({"foo": 1}) == [], "无规则返回空列表")
check(mod._extract_rules("notdict") == [], "非 dict 返回空列表")
q = mod._urlencode({"a": "1", "scene": "ENV_FILTER_GUANGYA"})
check("scene=ENV_FILTER_GUANGYA" in q, "urlencode 包含 scene 参数")

# ---------------------------------------------------------------------------
# 2) _emby_has_item 三分支（mock 模块内 _http_json）
# ---------------------------------------------------------------------------
print("\n== _emby_has_item 分支 ==")

EMBY_STATE = {"v": "exact_empty"}  # 默认：精确查无，看兜底


def fake_emby_http(url, token=None, method="GET", payload=None, timeout=20):
    if "AnyProviderIdEquals" in url:
        if EMBY_STATE["v"] == "exact_hit":
            return 200, {"Items": [{"Id": "1", "Name": "X",
                                    "ProviderIds": {"Tmdb": "123"},
                                    "ProductionYear": 2024}]}, ""
        if EMBY_STATE["v"] == "exact_unreachable":
            return 0, None, "conn refused"
        return 200, {"Items": []}, ""  # exact_empty
    # 兜底搜索分支
    if EMBY_STATE["v"] == "search_hit_tmdb":
        return 200, {"Items": [{"Name": "X", "ProviderIds": {"Tmdb": "123"},
                                "ProductionYear": 2024}]}, ""
    if EMBY_STATE["v"] == "search_hit_name":
        return 200, {"Items": [{"Name": "Cool Movie", "ProviderIds": {},
                                "ProductionYear": 2024}]}, ""
    if EMBY_STATE["v"] == "search_unreachable":
        return 0, None, "conn"
    return 200, {"Items": []}, ""  # search_empty


orig_http = mod._http_json
mod._http_json = fake_emby_http

# 精确命中
EMBY_STATE["v"] = "exact_hit"
check(mod._emby_has_item("http://127.0.0.1:8096", "k", 123, "X", "movie", 2024) is True,
      "精确匹配 Tmdb 命中 -> True")

# 精确无，兜底按名称+年份命中
EMBY_STATE["v"] = "search_hit_name"
check(mod._emby_has_item("http://127.0.0.1:8096", "k", 123, "Cool Movie", "movie", 2024) is True,
      "兜底 名称+年份 命中 -> True")

# 精确无，兜底无 -> False
EMBY_STATE["v"] = "search_empty"
check(mod._emby_has_item("http://127.0.0.1:8096", "k", 123, "Cool Movie", "movie", 2024) is False,
      "均未命中 -> False")

# 兜底接口不可达 -> None（保守）
EMBY_STATE["v"] = "search_unreachable"
check(mod._emby_has_item("http://127.0.0.1:8096", "k", 123, "Cool Movie", "movie", 2024) is None,
      "Emby 不可达（兜底失败）-> None（保守不删）")

# emby_url 为空 -> None
check(mod._emby_has_item("", "k", 123, "X", "movie", 2024) is None,
      "emby_url 为空 -> None")

mod._http_json = orig_http  # 恢复

# ---------------------------------------------------------------------------
# 3) run() 集成：清理权 / 阈值 / 兜底
# ---------------------------------------------------------------------------
print("\n== run() 集成：清理权与阈值 ==")

# 受控 _emby_has_item：按 tmdb_id 查表
EMBY_MAP = {}
orig_emby = mod._emby_has_item


def fake_emby(tmdb_result_map):
    def _f(emby_url, emby_key, tmdb_id, title, media_type, year, timeout=15):
        return tmdb_result_map.get(tmdb_id)
    return _f


# 受控 _http_json：GET 返回白名单，POST 捕获 payload
GET_CFG = {}
POST_CAP = {}


def fake_http_run(url, token=None, method="GET", payload=None, timeout=20):
    if method == "POST":
        POST_CAP["payload"] = payload
        return 200, {}, ""
    return 200, GET_CFG, ""


mod._http_json = fake_http_run
mod._emby_has_item = fake_emby(EMBY_MAP)

# 用可控时间：把模块内的 time 替换为带固定 now 的命名空间
FakeTime = types.SimpleNamespace(time=lambda: NOW)
mod.time = FakeTime
NOW = 1_000_000.0


def run_once(data_dir, cleanup_days=5, cfg_extra=None):
    params = {"tgtdrive_base_url": "http://127.0.0.1:12366",
              "tgtdrive_token": "tok",
              "scene": "ENV_FILTER_GUANGYA",
              "whitelist_endpoint": "/api/visual-filter/config",
              "emby_url": "http://127.0.0.1:8096",
              "emby_key": "k",
              "cleanup_days": cleanup_days}
    if cfg_extra:
        params.update(cfg_extra)
    context = {"data_dir": data_dir}
    return mod.run(params, context)


# --- 场景 A：规则尚未到期（刚入库），不应删除 ---
print("\n-- 场景 A：刚入库未到期，不删除用户规则也不删 kw 规则 --")
tmpA = tempfile.mkdtemp()
GET_CFG = {"scene": "ENV_FILTER_GUANGYA",
           "rules": [kw_rule(111, 0, title="MovieA"), user_rule()],
           "raw_regex": "OLD", "advanced_raw_regex": "OLD2"}
EMBY_MAP.clear(); EMBY_MAP[111] = True  # 已入库
POST_CAP.clear()
res = run_once(tmpA)
check(res["ok"] is True, "run 返回 ok")
check(res["checked"] == 1, "检查到 1 条 kw_ 规则")
check(res["ingested"] == 1, "该规则已入库")
check(res["removed"] == 0, "未满 cleanup_days -> 未删除")
check("payload" not in POST_CAP, "无到期 -> 不发 POST 覆盖")
# stats.json 应记录入库计时
stats = json.loads(open(os.path.join(tmpA, "stats.json"), encoding="utf-8").read())
check("kw_111_0" in stats, "stats.json 记录了 kw_111_0 入库时间")

# --- 场景 B：同一规则满 cleanup_days，应删除该 kw 规则，保留用户规则 ---
print("\n-- 场景 B：满 cleanup_days，删除 kw 规则、保留用户规则、丢弃 raw_regex --")
NOW = 1_000_000.0 + 5 * 86400 + 10  # 推进 >5 天
POST_CAP.clear()
res = run_once(tmpA)  # 复用同一 data_dir，计时从 stats.json 读出
check(res["removed"] == 1, "到期 -> 删除 1 条 kw 规则")
check("payload" in POST_CAP, "到期 -> 发起 POST 覆盖")
post_rules = POST_CAP["payload"].get("rules", [])
rule_ids = [r["id"] for r in post_rules]
check("user_rule_1" in rule_ids, "用户规则被保留（未被清理权误删）")
check("kw_111_0" not in rule_ids, "到期 kw_111_0 已被移除")
check(POST_CAP["payload"].get("raw_regex") in (None, ""), "POST 丢弃陈旧 raw_regex")
check(POST_CAP["payload"].get("advanced_raw_regex") in (None, ""), "POST 丢弃 advanced_raw_regex")
check(POST_CAP["payload"].get("scene") == "ENV_FILTER_GUANGYA", "POST 带正确 scene")

# --- 场景 C：Emby 不可达（None）应保守不删、不计时 ---
print("\n-- 场景 C：Emby 不可达 -> 保守不删 --")
tmpC = tempfile.mkdtemp()
GET_CFG = {"scene": "ENV_FILTER_GUANGYA",
           "rules": [kw_rule(222, 0, title="MovieC")]}
EMBY_MAP.clear(); EMBY_MAP[222] = None  # 不可达
POST_CAP.clear()
res = run_once(tmpC)
check(res["removed"] == 0, "不可达 -> 不删除")
check("payload" not in POST_CAP, "不可达 -> 不发 POST")
statsC = json.loads(open(os.path.join(tmpC, "stats.json"), encoding="utf-8").read())
check("kw_222_0" not in statsC, "不可达 -> 不记录计时（保留后续机会）")

# --- 场景 D：未入库（False）应清掉计时、不删除 ---
print("\n-- 场景 D：未入库 -> 清计时、不删除 --")
tmpD = tempfile.mkdtemp()
GET_CFG = {"scene": "ENV_FILTER_GUANGYA",
           "rules": [kw_rule(333, 0, title="MovieD")]}
EMBY_MAP.clear(); EMBY_MAP[333] = False  # 未入库
# 先模拟一轮：假设之前已记过时（预置 stats）
open(os.path.join(tmpD, "stats.json"), "w", encoding="utf-8").write(
    json.dumps({"kw_333_0": 1_000_000.0}))
POST_CAP.clear()
res = run_once(tmpD)
check(res["removed"] == 0, "未入库 -> 不删除")
statsD = json.loads(open(os.path.join(tmpD, "stats.json"), encoding="utf-8").read())
check("kw_333_0" not in statsD, "未入库 -> 清除已有计时")
check("payload" not in POST_CAP, "未入库 -> 不发 POST")

# --- 场景 E：cleanup_days<=0 直接跳过 ---
print("\n-- 场景 E：cleanup_days<=0 跳过 --")
tmpE = tempfile.mkdtemp()
GET_CFG = {"scene": "ENV_FILTER_GUANGYA",
           "rules": [kw_rule(444, 0, title="MovieE")]}
EMBY_MAP.clear(); EMBY_MAP[444] = True
POST_CAP.clear()
res = run_once(tmpE, cleanup_days=0)
check(res.get("skipped") is True, "cleanup_days<=0 -> skipped")

# 恢复模块原始函数（避免影响后续 import 的其它测试）
mod._http_json = orig_http
mod._emby_has_item = orig_emby
import time as _time
mod.time = _time

print("\n==== 结果：通过 %d / 失败 %d ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
