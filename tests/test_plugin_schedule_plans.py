# -*- coding: utf-8 -*-
"""2.5.1 插件定时执行（cron 计划）+ 两个运行时错误回归契约（纯标准库）。

覆盖：
* cron 5 段解析（* / 步长 / 范围 / 列表 / 星期别名 / 日周 OR）与非法表达式拒绝；
* 北京时间 UTC+8 的下次执行计算（不受宿主 TZ 影响）；
* 计划存储 CRUD、原子落盘、损坏文件容错、结果记录上限；
* 「上一轮仍在运行或排队时跳过本次触发」与「服务离线期间不补跑」；
* **回归契约（本次两个线上故障）**：router 里所有 ``from app.plugins import X``
  的名字必须在包中真实导出；所有被调用的裸名字必须有导入（防止 ImportError /
  NameError 导致的接口 500 —— 纯文本契约测试此前只查字符串，抓不到这类错误）；
* 前端截图风格面板、后端端点、管理员守卫、单 script 标签等静态契约。

不引入任何第三方依赖：schedule_plans.py 顶层只依赖标准库，用 runpy 直接加载。
"""
import ast
import datetime
import json
import os
import runpy
import sys
import tempfile
import threading
import time as _time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SP = runpy.run_path(os.path.join(ROOT, "app", "plugins", "schedule_plans.py"))

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


TZ = SP["TZ_BEIJING"]
parse_cron = SP["parse_cron"]
next_ts = SP["next_ts"]
fmt_ts = SP["fmt_ts"]
PlanError = SP["PlanError"]
PlanStore = SP["PlanStore"]
PlanRuntime = SP["PlanRuntime"]

# 截图示例基准：北京时间 2026/11/07（周六）09:00
BASE_DT = datetime.datetime(2026, 11, 7, 9, 0, tzinfo=TZ)
BASE = BASE_DT.timestamp()

print("== 1. cron 解析：5 段 / 步长 / 范围 / 列表 / 星期别名 ==")
check(fmt_ts(next_ts("0 9 * * *", BASE)) == "2026/11/08 09:00",
      "截图示例 0 9 * * * -> 次日 09:00（北京时间）")
check(fmt_ts(next_ts("*/5 * * * *", BASE)) == "2026/11/07 09:05", "*/5 步长")
check(fmt_ts(next_ts("30 9 * * 1-5", BASE)) == "2026/11/09 09:30",
      "工作日范围 1-5：周六顺延至周一 09:30")
check(fmt_ts(next_ts("0 0 1,15 * *", BASE)) == "2026/11/15 00:00", "列表 1,15")
check(fmt_ts(next_ts("0 8-20/4 * * *", BASE)) == "2026/11/07 12:00", "范围步长 8-20/4")
check(fmt_ts(next_ts("0 9 * * sun", BASE)) == "2026/11/08 09:00",
      "星期别名 sun 等价于 0（次日周日）")
check(fmt_ts(next_ts("0 9 8 * *", BASE)) == "2026/11/08 09:00", "指定日 8 号")
# 日 / 周 OR 语义（标准 cron）：1 号 或 周一，取更早者；
# 基准是 11/07 周六，下一次命中是 11/09 周一 09:00（而非 12/01）
check(fmt_ts(next_ts("0 9 1 * 1", BASE)) == "2026/11/09 09:00",
      "日与周同时限定时取 OR，命中最近的周一")

bad_exprs = ["", "* * * *", "* * * * * *", "99 * * * *", "0 24 * * *",
            "0 9 * * 9", "abc * * * *", "*/0 * * * *", "0 9 * 13 *",
            "0 9 32 * *", "0 9 * * ", "x" * 100]
for expr in bad_exprs:
    try:
        parse_cron(expr)
        check(False, "非法 cron 应被拒绝: %r" % expr)
    except PlanError:
        check(True, "非法 cron 被拒绝: %r" % expr)

print("\n== 2. UTC+8 与宿主时区无关 ==")
old_tz = os.environ.get("TZ")
try:
    os.environ["TZ"] = "UTC"
    if hasattr(_time, "tzset"):
        _time.tzset()
    check(fmt_ts(BASE) == "2026/11/07 09:00", "TZ=UTC 时仍按北京时间格式化")
    check(fmt_ts(next_ts("0 9 * * *", BASE)) == "2026/11/08 09:00",
          "TZ=UTC 时 cron 仍按北京时间触发")
finally:
    if old_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old_tz
    if hasattr(_time, "tzset"):
        _time.tzset()
check(SP["fmt_ts"](None) == "" and SP["fmt_ts"](0) == "", "空时间戳格式化为空串")

print("\n== 3. 输入校验：防模板注入 / 非法插件 ID ==")
validate = SP["validate_plan_input"]
for payload in (
    {"name": "<script>x</script>", "plugin_id": "hello_plugin", "cron": "0 9 * * *"},
    {"name": "ok", "plugin_id": "../evil", "cron": "0 9 * * *"},
    {"name": "ok", "plugin_id": "hello_plugin", "cron": "* * * *"},
    {"name": "x" * 200, "plugin_id": "hello_plugin", "cron": "0 9 * * *"},
    {"name": "", "plugin_id": "hello_plugin", "cron": "0 9 * * *"},
    {"name": "ok", "plugin_id": "hello_plugin", "cron": "0 9 * * *", "description": "a&b"},
):
    try:
        validate(payload)
        check(False, "非法计划输入应被拒绝: %r" % (payload,))
    except PlanError:
        check(True, "非法计划输入被拒绝")
ok_payload = {"name": "完结关键词清理", "description": "求片完结关键词清理",
              "plugin_id": "hello_plugin", "cron": "0 9 * * *"}
check(validate(ok_payload)["enabled"] is True, "合法输入默认启用")
check("name" not in validate({"enabled": False}, partial=True),
      "partial=True 只校验出现的字段")

print("\n== 4. 存储 CRUD / 原子落盘 / 结果记录 ==")
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "nested", "plans.json")
    clock = {"t": BASE}
    store = PlanStore(path, clock=lambda: clock["t"])
    check(store.list_plans() == [], "文件不存在时为空列表")
    plan = store.create_plan(ok_payload)
    pid = plan["id"]
    check(bool(SP["PLAN_ID_RE"].match(pid)), "计划 ID 形如 plan_xxxxxxxx")
    check(plan["next_run_at"] is not None and fmt_ts(plan["next_run_at"]) == "2026/11/08 09:00",
          "创建时即计算下次执行")
    check(os.path.isfile(path), "计划文件原子落盘到嵌套目录")
    with open(path, encoding="utf-8") as fp:
        check(json.load(fp)["plans"][0]["name"] == "完结关键词清理", "落盘内容为合法 JSON")
    check(PlanStore(path, clock=lambda: clock["t"]).get_plan(pid)["cron"] == "0 9 * * *",
          "重新加载可读取计划（重启不丢）")

    edited = store.update_plan(pid, {"cron": "0 10 * * *", "description": "改为每天十点"})
    check(edited["cron"] == "0 10 * * *" and edited["description"] == "改为每天十点", "编辑生效")
    check(fmt_ts(edited["next_run_at"]) == "2026/11/07 10:00", "编辑 cron 后重算下次执行")
    check(store.update_plan("plan_missing", {"name": "x"}) is None, "编辑不存在的计划返回 None")

    for i in range(SP["MAX_RESULTS"] + 5):
        store.record_result(pid, "success", "第 %d 次" % i, i, "manual")
    rows = store.results_of(pid)
    check(len(rows) == SP["MAX_RESULTS"], "结果记录上限 %d 条" % SP["MAX_RESULTS"])
    check(rows[0]["message"] == "第 %d 次" % (SP["MAX_RESULTS"] + 4)
          and rows[0]["status"] == "success",
          "结果按时间倒序且保留最新一条")
    got = store.get_plan(pid)
    check(got["last_status"] == "success" and got["last_duration_ms"] >= 0,
          "计划同步最近触发摘要")
    try:
        store.update_plan(pid, {"cron": "bad cron"})
        check(False, "非法 cron 编辑应被拒绝")
    except PlanError:
        check(True, "非法 cron 编辑被拒绝")

    check(store.delete_plan(pid) is True and store.delete_plan(pid) is False,
          "删除计划幂等返回")
    check(store.list_plans() == [], "删除后列表为空")

    # 损坏文件容错
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("{ broken json")
    check(PlanStore(path, clock=lambda: clock["t"]).list_plans() == [],
          "损坏的计划文件降级为空列表，不抛异常")

print("\n== 5. 防并发跳过 / 离线不补跑 / 到点推进 ==")
with tempfile.TemporaryDirectory() as td:
    clock = {"t": BASE}
    store = PlanStore(os.path.join(td, "plans.json"), clock=lambda: clock["t"])
    p = store.create_plan({"name": "测试", "plugin_id": "hello_plugin", "cron": "0 9 * * *"})
    pid = p["id"]
    calls = []

    def slow_executor(plan):
        calls.append(plan["id"])
        threading.Event().wait(0.05)
        return True, "运行成功"

    runtime = PlanRuntime(store, slow_executor, clock=lambda: clock["t"])
    # 手动把 next_run_at 设到当前，构造「到点」
    store.set_next_run(pid, BASE)
    check(store.due_ids(BASE) == [pid], "到点计划被 due_ids 选中")
    ran = runtime.tick(now=BASE)
    check(ran == [pid] and calls == [pid], "tick 触发并执行插件动作")
    check(store.results_of(pid)[0]["status"] == "success", "运行成功写入结果")
    check(fmt_ts(store.get_plan(pid)["next_run_at"]) == "2026/11/08 09:00",
          "触发后推进到下一次，不在同一分钟重复")
    check(store.due_ids(BASE) == [], "推进后不再重复触发")

    # 并发：executor 阻塞在另一线程时，tick 应跳过本次
    store.set_next_run(pid, BASE)
    blocker = threading.Event()
    calls.clear()

    def blocking_executor(plan):
        calls.append(plan["id"])
        blocker.wait(0.4)
        return True, "运行成功"

    runtime2 = PlanRuntime(store, blocking_executor, clock=lambda: clock["t"])
    th = threading.Thread(target=lambda: runtime2.run_plan(pid, "manual"))
    th.start()
    while not calls:
        threading.Event().wait(0.005)
    check(runtime2.is_running(pid), "运行中的计划处于 running 集合")
    before = len(calls)
    ticked = runtime2.tick(now=BASE)
    check(ticked == [] and len(calls) == before, "上一轮仍在运行时跳过本次触发，不重复执行")
    statuses = [r["status"] for r in store.results_of(pid)]
    check("skipped" in statuses, "跳过时写入 skipped 结果")
    blocker.set()
    th.join(timeout=2)

    # 离线 3 天：reconcile 直接推进到未来，不补跑
    clock["t"] = BASE + 3 * 86400
    p3 = store.create_plan({"name": "离线", "plugin_id": "hello_plugin", "cron": "0 9 * * *"})
    store.set_next_run(p3["id"], BASE)
    changed = store.reconcile(BASE + 3 * 86400)
    check(changed >= 1, "reconcile 处理过期计划")
    nxt = store.get_plan(p3["id"])["next_run_at"]
    check(nxt is not None and nxt > BASE + 3 * 86400,
          "服务离线期间不补跑：下次执行直接落到当前之后")

print("\n== 6. 回归契约：router 里所有 from app.plugins import 的名字必须真实导出 ==")
router_src = read("app/domains/plugins/router.py")
init_src = read("app/plugins/__init__.py")

init_tree = ast.parse(init_src)
exported = set()
for node in init_tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        exported.add(node.name)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                exported.add(target.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        exported.add(node.target.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            exported.add(alias.asname or alias.name.split(".")[0])

check("get_plugin_scheduler" in exported,
      "包级导出 get_plugin_scheduler（修复定时面板接口 ImportError 500）")
check("refresh_external_manifests" in exported, "包级导出 refresh_external_manifests")

# from app.plugins import X 合法名字 = 包级导出 ∪ app/plugins/ 下的子模块名
submodules = {
    os.path.splitext(name)[0]
    for name in os.listdir(os.path.join(ROOT, "app", "plugins"))
    if name.endswith(".py") and not name.startswith("__")
}
resolvable = exported | submodules
router_tree = ast.parse(router_src)
bad_imports = []
for node in ast.walk(router_tree):
    if isinstance(node, ast.ImportFrom) and node.module == "app.plugins":
        for alias in node.names:
            if alias.name not in resolvable:
                bad_imports.append((node.lineno, alias.name))
check(not bad_imports,
      "router 所有 from app.plugins import 均可解析（越界: %s）" % bad_imports)

# 所有被调用的裸名字必须在「模块级或函数内导入 / 内建 / 局部定义」中可解析
_BUILTINS = set(dir(__builtins__)) if not isinstance(__builtins__, dict) else set(__builtins__)
module_bound = set()
for node in router_tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        module_bound.add(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                module_bound.add(t.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            module_bound.add(a.asname or a.name.split(".")[0])

undefined = []
for fn in [n for n in router_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
    bound = set(module_bound) | set(a.arg for a in fn.args.args)
    for node in ast.walk(fn):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(node, (ast.For, ast.comprehension)) and isinstance(getattr(node, "target", None), ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name not in bound and name not in _BUILTINS:
                undefined.append((fn.name, name, node.lineno))
check(not undefined,
      "router 无未定义的裸名字调用（防止卸载接口 NameError 500）: %s" % undefined)

print("\n== 7. 卸载接口：refresh 已导入且清理失败不影响成功响应 ==")
uninstall_fn = next(n for n in router_tree.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "api_uninstall_plugin")
uninstall_src = ast.get_source_segment(router_src, uninstall_fn) or ""
check("from app.plugins import uninstall_plugin, plugin_dao, refresh_external_manifests" in uninstall_src,
      "卸载接口显式导入 refresh_external_manifests")
check("try:" in uninstall_src and "except Exception:" in uninstall_src,
      "卸载后的清理步骤做异常隔离（磁盘已删除不得翻转成 500）")
check(uninstall_src.count("return {\"status\": \"success\"") >= 2,
      "清理失败时仍返回 success（消除「假失败」）")

print("\n== 8. 后端端点与启动钩子契约 ==")
for path in (
    "/api/plugins/plans",
    "/api/plugins/plans/{plan_id}",
    "/api/plugins/plans/{plan_id}/toggle",
    "/api/plugins/plans/{plan_id}/run",
    "/api/plugins/plans/{plan_id}/results",
    # 2.5 旧端点保留，不删特性
    "/api/plugins/schedules",
    "/api/plugins/{plugin_id}/schedule",
    "/api/plugins/{plugin_id}/run-schedule",
):
    check('"%s"' % path in router_src, "端点存在: %s" % path)
check(router_src.count("_admin_ok(request)") >= 12, "新增端点均有管理员判定")
check('from app.plugins.scheduler import get_plugin_scheduler' in router_src,
      "旧定时接口显式从子模块导入调度器单例")
bootstrap_src = read("app/bootstrap/plugin_routes.py")
check("start_plugin_plan_scheduler" in bootstrap_src
      and "start_plugin_scheduler()" in bootstrap_src,
      "两个调度器均随应用启动（旧按分钟 + 新 cron 计划）")

print("\n== 9. 前端截图风格面板与单 script 标签 ==")
ui = read("templates/plugins.html")
for label, needle in (
    ("标题 插件定时执行", "插件定时执行"),
    ("UTC+8 说明", "cron 按北京时间（UTC+8）执行"),
    ("跳过并发说明", "上一轮仍在运行或排队时跳过本次触发"),
    ("离线不补跑说明", "服务离线期间不补跑"),
    ("N 个定时计划", "schedule-plan-count"),
    ("刷新计划", "刷新计划"),
    ("新增计划", "新增计划"),
    ("cron 表达式框", "plan-cron"),
    ("参数复用说明", "参数：复用插件已保存的设置"),
    ("下次执行", "下次执行："),
    ("最近触发 + 运行结果", "最近触发："),
    ("编辑", "openPluginPlanEditor"),
    ("停用", "togglePluginPlan"),
    ("查看结果", "viewPluginPlanResults"),
    ("立即执行", "runPluginPlanNow"),
    ("删除", "deletePluginPlan"),
    ("计划列表接口", "/api/plugins/plans"),
):
    check(needle in ui, "前端含：%s" % label)
check(ui.count("<script") == 1, "前端仅一个 script 标签（沿用 CSP nonce）")
check("schedule-list" not in ui and "savePluginSchedule" not in ui,
      "旧的按分钟面板已完全移除，无死代码")

print("\n== 10. 新模块顶层只依赖标准库 ==")
_stdlib = {
    "__future__", "json", "os", "re", "threading", "time", "uuid", "datetime",
    "typing", "collections", "functools", "itertools", "math", "hashlib", "io",
}
tree = ast.parse(read("app/plugins/schedule_plans.py"))
imports = set()
for node in tree.body:
    if isinstance(node, ast.Import):
        imports.update(a.name for a in node.names)
    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        imports.add(node.module)
outside = sorted(i for i in imports
                 if i.split(".")[0] not in _stdlib and i != "__future__")
check(not outside, "schedule_plans.py 顶层只依赖标准库（越界: %s）" % outside)
check("from app.plugins import _registry" in read("app/plugins/schedule_plans.py"),
      "插件执行逻辑延迟导入，保持模块可独立加载")

print("\npassed: %d  failed: %d" % (PASS, len(FAILS)))
if FAILS:
    for msg in FAILS:
        print("  - " + msg)
    sys.exit(1)
print("PLUGIN SCHEDULE PLANS CHECKS PASSED")
