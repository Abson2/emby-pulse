# -*- coding: utf-8 -*-
"""2.5 插件中心能力回归（纯标准库）。

覆盖：
* 定时执行调度器（scheduler.py）的纯函数调度逻辑（假时钟）；
* 开发资源（dev_resources.py）的文档读取、示例 ZIP 生成与「通过 2.3 全部安全校验」；
* 导入后即时刷新内存 manifest 的静态契约（修复「导入后不显示」）；
* 插件中心前端 8 项 + OpenAPI 入口的静态契约。

不引入任何第三方依赖：scheduler / dev_resources 用 runpy 直接加载纯标准库模块，
其余为源码文本形态契约，与 test_plugin_import.py 同一思路。
"""
import ast
import io
import json
import os
import runpy
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = runpy.run_path(os.path.join(ROOT, "app", "plugins", "external.py"))
SCH = runpy.run_path(os.path.join(ROOT, "app", "plugins", "scheduler.py"))
DR = runpy.run_path(os.path.join(ROOT, "app", "plugins", "dev_resources.py"))

PASS = 0
FAILS = []


def check(cond, msg):
    global PASS
    if cond:
        PASS += 1
        print("  PASS  " + msg)
    else:
        FAILS.append(msg)
        print("  FAIL  " + msg)


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fp:
        return fp.read()


print("== 1. 调度器：due_ids / tick / mark_ran / next_due_in（假时钟） ==")
PluginScheduler = SCH["PluginScheduler"]
TICK = SCH["TICK_SECONDS"]
MAX_MIN = SCH["MAX_MINUTES"]

calls = []


def make_clock(seq):
    it = iter(seq)
    def clock():
        try:
            return next(it)
        except StopIteration:
            return 10 ** 9
    return clock


# 单条目：60s 间隔，初始 started_at=0
entries = [{"id": "p1", "minutes": 1, "runnable": True}]
s = PluginScheduler(lambda: entries, lambda pid: calls.append(pid), clock=lambda: 0)
# now=100 -> 100-0 >= 60 应到点
check(s.due_ids(now=100) == ["p1"], "超过 60s 间隔即到点")
# minutes=0 永不触发
entries2 = [{"id": "p0", "minutes": 0, "runnable": True}]
s2 = PluginScheduler(lambda: entries2, lambda pid: None, clock=lambda: 0)
check(s2.due_ids(now=10 ** 9) == [], "minutes=0 永不触发")
# runnable=False 即使配了间隔也不触发
entries3 = [{"id": "p3", "minutes": 1, "runnable": False}]
s3 = PluginScheduler(lambda: entries3, lambda pid: None, clock=lambda: 0)
check(s3.due_ids(now=10 ** 9) == [], "runnable=False 不触发")
# tick 触发并标记 last_run
calls.clear()
ran = s.tick(now=100)
check(ran == ["p1"] and calls == ["p1"], "tick 触发并执行 run_hook")
check(s.last_run_of("p1") == 100, "tick 后记录 last_run")
# next_due_in 计算：last=100，minutes=1；now=120 -> 40s；now=100 -> 60s（未到）
check(s.next_due_in("p1", 1, now=120) == 40, "next_due_in 正确计算剩余秒数")
check(s.next_due_in("p1", 1, now=100) == 60, "next_due_in 边界：恰好间隔时归零前剩 60s")
check(s.next_due_in("p1", 0, now=120) is None, "minutes=0 时 next_due_in 为 None")
# 紧接下一次 tick（now=120，距上次 20s<60）不应再触发
calls.clear()
check(s.tick(now=120) == [] and calls == [], "未到间隔不重复触发")
# 距上次 60s 后应再次触发
calls.clear()
check(s.tick(now=160) == ["p1"], "间隔再次到点触发")
# 手动 mark_ran 后 next_due_in 从新基准算起
s.mark_ran("p1", now=200)
check(s.next_due_in("p1", 1, now=200) == 60, "mark_ran 重置 last_run 基准")
# 超大 minutes 被钳制到 MAX_MINUTES
big = [{"id": "big", "minutes": 999999, "runnable": True}]
sb = PluginScheduler(lambda: big, lambda pid: None, clock=lambda: 0)
check(sb.due_ids(now=MAX_MIN * 60 - 1) == [], "超上限 minutes 钳制：未到 MAX 不触发")
check(sb.due_ids(now=MAX_MIN * 60) == ["big"], "超上限 minutes 钳制：到 MAX 触发")
# stop/start 不抛异常（线程健壮性）
try:
    s.start(); s.stop(); check(True, "调度器 start/stop 不抛异常")
except Exception as exc:
    check(False, "调度器 start/stop 不抛异常: %r" % exc)

print("\n== 2. 开发资源：示例 ZIP 通过 2.3 全部安全校验 ==")
zip_bytes = DR["build_example_zip"]()
check(isinstance(zip_bytes, bytes) and len(zip_bytes) > 0, "build_example_zip 返回非空字节")
try:
    files = EXT["validate_zip"](zip_bytes)
    check("manifest.json" in files and "plugin.py" in files and "README.md" in files,
          "示例包含 manifest.json / plugin.py / README.md")
    raw = json.loads(files["manifest.json"].decode("utf-8"))
    mf = EXT["validate_manifest"](raw, reserved=frozenset())
    check(mf["id"] == "hello_plugin" and mf["entry"] == "plugin.py",
          "示例 manifest 通过校验且含必填 entry")
except Exception as exc:
    check(False, "示例包通过 2.3 校验: %r" % exc)

print("\n== 3. 示例 ZIP 端到端安装（离线、临时目录） ==")
old_env = os.environ.get("PLUGIN_EXTERNAL_DIR")
_td = tempfile.TemporaryDirectory()
os.environ["PLUGIN_EXTERNAL_DIR"] = _td.name
try:
    res = EXT["install_from_zip"](zip_bytes, {"type": "local"})
    check(res["manifest"]["id"] == "hello_plugin", "示例包可安装")
    check(EXT["list_installed"]().get("hello_plugin", {}).get("id") == "hello_plugin",
          "安装后 list_installed 可枚举")
    check(EXT["uninstall"]("hello_plugin") is True, "示例包可卸载")
finally:
    if old_env is None:
        os.environ.pop("PLUGIN_EXTERNAL_DIR", None)
    else:
        os.environ["PLUGIN_EXTERNAL_DIR"] = old_env
    _td.cleanup()

print("\n== 4. 开发文档读取与缺失降级 ==")
guide = DR["get_dev_guide"]()
ai = DR["get_ai_prompt"]()
check(isinstance(guide, str) and len(guide) > 0, "get_dev_guide 返回非空（docs 随仓库部署）")
check(isinstance(ai, str) and len(ai) > 0, "get_ai_prompt 返回非空")
check("on_schedule" in DR["EXAMPLE_PLUGIN_PY"] and "plugin = HelloPlugin()" in DR["EXAMPLE_PLUGIN_PY"],
      "单文件示例含 on_schedule 钩子与模块级实例")
# 文档读取失败时（文件不存在 / docs 未随部署）应返回空串且不崩
check(DR["_read_doc"]("NONEXISTENT_FILE.md") == "",
      "文档缺失时返回空串（前端给可读提示），不抛异常")

print("\n== 5. 导入后即时刷新 manifest 静态契约（修复「导入后不显示」） ==")
router_src = read("app/domains/plugins/router.py")
init_src = read("app/plugins/__init__.py")
check("def refresh_external_manifests" in init_src, "加载器新增 refresh_external_manifests")
check('from app.plugins import refresh_external_manifests' in router_src,
      "导入接口调用 refresh_external_manifests")
# 两个导入端点（本地 / github）各调用一次 + 卸载一次 = >=3
n = router_src.count("refresh_external_manifests()")
check(n >= 3, "refresh 在本地导入 / GitHub 导入 / 卸载三处均被调用（实际 %d 处）" % n)
check("get_all_plugins" in router_src or "get_all_plugins()" in router_src,
      "插件列表接口复用 get_all_plugins（含刷新后的外部 manifest）")

print("\n== 6. 2.5 后端端点静态契约 ==")
for path in (
    "/api/plugins/dev/ai-prompt", "/api/plugins/dev/guide",
    "/api/plugins/dev/example-zip", "/api/plugins/dev/example-single",
    "/api/plugins/schedules", "/api/plugins/{plugin_id}/schedule",
    "/api/plugins/{plugin_id}/run-schedule",
):
    check('"%s"' % path in router_src, "2.5 端点存在: %s" % path)
check(router_src.count("_admin_ok(request)") >= 7,
      "2.5 新增端点均做管理员判定（纵深防御）")
check('schedule_minutes' in router_src, "定时间隔存于插件配置 schedule_minutes（复用既有配置，不增表）")

print("\n== 7. 启动钩子：全局调度器随应用启动 ==")
routes_src = read("app/bootstrap/plugin_routes.py")
check("from app.plugins.scheduler import start_plugin_scheduler" in routes_src
      and "start_plugin_scheduler()" in routes_src,
      "register_plugin_routes 幂等启动全局调度器")

print("\n== 8. 插件中心前端 8 项 + OpenAPI 入口静态契约 ==")
ui = read("templates/plugins.html")
# 8 项需求：插件市场 / 定时执行 / AI 开发提示词 / 开发指南 / ZIP 开发示例 / 单文件.py 示例 / 刷新 / 导入插件
checks = {
    "插件市场": ("openPluginMarket(" in ui) or ("插件市场" in ui),
    "定时执行": ("openPluginSchedulePanel(" in ui) or ("定时执行" in ui),
    "AI 开发提示词": ("openPluginDevDoc('ai-prompt')" in ui) or ("AI 开发提示词" in ui) or ("AI开发提示词" in ui),
    "开发指南": ("openPluginDevDoc('guide')" in ui) or ("开发指南" in ui),
    "ZIP 开发示例": ("downloadPluginExample('zip')" in ui) or ("ZIP 开发示例" in ui) or ("ZIP开发示例" in ui),
    "单文件.py 示例": ("downloadPluginExample('single')" in ui) or ("单文件" in ui),
    "刷新": ("function loadPlugins(" in ui) or ("loadPlugins(" in ui),
    "导入插件": ("openPluginImportModal(" in ui) or ("submitLocalPlugin(" in ui) or ("导入插件" in ui),
    "OpenAPI 入口": ("openOpenApiPanel(" in ui) and ("创建访问令牌" in ui) and ("下载 OpenAPI 规范" in ui),
}
for label, ok in checks.items():
    check(ok, "插件中心含：%s" % label)
check(ui.count("<script") == 1, "前端仅一个 script 标签（沿用 CSP nonce）")

print("\n== 9. 两个新模块只依赖标准库（顶层 import） ==")
_stdlib = {
    "__future__", "hashlib", "io", "json", "os", "re", "shutil", "stat", "sys", "uuid",
    "zipfile", "datetime", "urllib", "urllib.error", "urllib.parse", "urllib.request",
    "importlib", "importlib.util", "typing", "threading", "time",
}
for rel in ("app/plugins/scheduler.py", "app/plugins/dev_resources.py"):
    tree = ast.parse(read(rel))
    imports = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
    outside = sorted(i for i in imports if i.split(".")[0] not in _stdlib and i != "__future__")
    check(not outside, "%s 顶层只依赖标准库（越界: %s）" % (rel, outside))

print("\npassed: %d  failed: %d" % (PASS, len(FAILS)))
if FAILS:
    for msg in FAILS:
        print("  - " + msg)
    sys.exit(1)
print("PLUGIN CENTER 2.5 CHECKS PASSED")
