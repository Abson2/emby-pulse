import time

from fastapi import APIRouter, Request, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from app.core.config import templates
from app.domains.users import public_service as user_service
from app.shared.view_context import get_common_vars
from app.plugins import get_all_plugins, set_plugin_enabled, get_plugin_config, save_plugin_config, update_plugin_config, get_plugin, get_plugin_logs, clear_plugin_logs, _registry

router = APIRouter()


def is_pro_user() -> bool:
    """检查是否为 Pro 用户"""
    return True

# 全局 app 引用，用于动态注册路由
_app = None

def set_app(app):
    """设置 FastAPI app 引用，用于动态注册路由"""
    global _app
    _app = app


@router.get("/plugins")
async def plugins_page(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    
    # 权限检查
    if not user_service.check_permission(request, "plugins"):
        return RedirectResponse("/?no_permission=1", status_code=303)
    
    return templates.TemplateResponse(request, "plugins.html", get_common_vars(request, "plugins"))


@router.get("/api/plugins")
def api_list_plugins(request: Request):
    # 🔒 安全检查：必须管理员
    if not user_service.is_admin_user(request):
        return {"status": "error", "message": "需要管理员权限"}
    return {"status": "success", "data": get_all_plugins()}


# ==========================================
# 2.5.1 定时执行（cron 计划）：北京时间 UTC+8 / 防并发 / 离线不补跑
# 静态前缀置于所有 /api/plugins/{plugin_id}/... 动态路由之前，避免被抢占匹配。
# ==========================================

def _plans_module():
    from app.plugins import schedule_plans
    return schedule_plans


def _plan_runtime():
    """取得计划运行时（bootstrap 未接线时按需幂等启动，避免接口 500）。"""
    sp = _plans_module()
    runtime = sp.get_plan_runtime()
    if runtime is None:
        runtime = sp.start_plugin_plan_scheduler()
    return runtime


def _plan_view(plan: dict, plugin_names: dict) -> dict:
    """把存储结构补成前端卡片所需的展示字段。"""
    sp = _plans_module()
    status_text = {
        "success": "运行成功", "failed": "运行失败", "skipped": "已跳过", "": "尚未运行",
    }.get(plan.get("last_status", ""), plan.get("last_status", ""))
    return {
        "id": plan["id"],
        "name": plan["name"],
        "description": plan.get("description", ""),
        "plugin_id": plan["plugin_id"],
        "plugin_name": plugin_names.get(plan["plugin_id"], plan["plugin_id"]),
        "cron": plan["cron"],
        "enabled": bool(plan.get("enabled")),
        "next_run_at": plan.get("next_run_at"),
        "next_run_text": sp.fmt_ts(plan.get("next_run_at")) or "—",
        "last_run_at": plan.get("last_run_at"),
        "last_run_text": sp.fmt_ts(plan.get("last_run_at")),
        "last_status": plan.get("last_status", ""),
        "last_status_text": status_text,
        "last_message": plan.get("last_message", ""),
        "last_duration_ms": plan.get("last_duration_ms", 0),
        "results_count": len(plan.get("results", [])),
    }


@router.get("/api/plugins/plans")
def api_list_plugin_plans(request: Request):
    """定时计划列表 + 可选插件下拉 + 服务器当前北京时间。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    sp = _plans_module()
    store = sp.get_plan_store()
    plugins = []
    names = {}
    for meta in get_all_plugins():
        names[meta["id"]] = meta.get("name", meta["id"])
        plugins.append({
            "id": meta["id"], "name": meta.get("name", meta["id"]),
            "enabled": bool(meta.get("enabled")),
            "loaded": bool(meta.get("loaded", True)),
            "external": bool(meta.get("external")),
        })
    data = [_plan_view(p, names) for p in store.list_plans()]
    return {
        "status": "success",
        "data": data,
        "plugins": plugins,
        "server_time": sp.fmt_ts(time.time()),
        "timezone": "UTC+8",
    }


@router.post("/api/plugins/plans")
async def api_create_plugin_plan(request: Request):
    """新增定时计划。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    sp = _plans_module()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return {"status": "error", "message": "请求数据格式错误"}
    try:
        plan = sp.get_plan_store().create_plan(payload)
    except sp.PlanError as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return {"status": "error", "message": "计划保存失败: %s" % exc}
    _plan_runtime()
    return {"status": "success", "data": _plan_view(plan, {}), "message": "计划已创建"}


@router.put("/api/plugins/plans/{plan_id}")
async def api_update_plugin_plan(plan_id: str, request: Request):
    """编辑计划（名称/描述/插件/cron/启停）。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    sp = _plans_module()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return {"status": "error", "message": "请求数据格式错误"}
    try:
        plan = sp.get_plan_store().update_plan(plan_id, payload)
    except sp.PlanError as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        return {"status": "error", "message": "计划更新失败: %s" % exc}
    if plan is None:
        return {"status": "error", "message": "计划不存在"}
    _plan_runtime()
    return {"status": "success", "data": _plan_view(plan, {}), "message": "计划已更新"}


@router.post("/api/plugins/plans/{plan_id}/toggle")
async def api_toggle_plugin_plan(plan_id: str, request: Request):
    """停用 / 启用计划（截图「停用」按钮）。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    sp = _plans_module()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    enabled = bool(payload.get("enabled")) if isinstance(payload, dict) else False
    plan = sp.get_plan_store().update_plan(plan_id, {"enabled": enabled})
    if plan is None:
        return {"status": "error", "message": "计划不存在"}
    return {"status": "success", "message": "已启用" if enabled else "已停用"}


@router.post("/api/plugins/plans/{plan_id}/run")
async def api_run_plugin_plan(plan_id: str, request: Request):
    """「立即执行」：与 cron 触发共用同一运行时，上一轮未结束则跳过。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    runtime = _plan_runtime()
    ok, message = await run_in_threadpool(runtime.run_plan, plan_id, "manual")
    return {"status": "success" if ok else "error", "message": message or "已触发"}


@router.get("/api/plugins/plans/{plan_id}/results")
def api_plugin_plan_results(plan_id: str, request: Request):
    """「查看结果」：最近 20 次运行记录（北京时间倒序）。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    sp = _plans_module()
    if sp.get_plan_store().get_plan(plan_id) is None:
        return {"status": "error", "message": "计划不存在"}
    rows = sp.get_plan_store().results_of(plan_id)
    status_text = {"success": "运行成功", "failed": "运行失败", "skipped": "已跳过"}
    for row in rows:
        row["at_text"] = sp.fmt_ts(row.get("at"))
        row["status_text"] = status_text.get(row.get("status", ""), row.get("status", ""))
        row["trigger_text"] = "手动" if row.get("trigger") == "manual" else "定时"
    return {"status": "success", "data": rows}


@router.delete("/api/plugins/plans/{plan_id}")
def api_delete_plugin_plan(plan_id: str, request: Request):
    """删除计划（不影响插件本身与其配置）。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    ok = _plans_module().get_plan_store().delete_plan(plan_id)
    if not ok:
        return {"status": "error", "message": "计划不存在"}
    return {"status": "success", "message": "计划已删除"}


@router.post("/api/plugins/{plugin_id}/toggle")
async def api_toggle_plugin(plugin_id: str, request: Request):
    # 🔒 安全检查：必须管理员
    if not user_service.is_admin_user(request):
        return {"status": "error", "message": "需要管理员权限"}
    data = await request.json()
    enabled = data.get("enabled", False)
    
    # Pro 插件权限检查
    plugin = _registry.get(plugin_id)
    if plugin and getattr(plugin, 'pro_only', False) and enabled and not is_pro_user():
        print(f"[🧩 插件] Pro 权限检查失败: plugin_id={plugin_id}, pro_only={getattr(plugin, 'pro_only', False)}")
        return {"status": "error", "message": "此插件需要 Pro 授权", "need_pro": True}
    
    print(f"[🧩 插件] toggle 请求: plugin_id={plugin_id}, enabled={enabled}")
    
    ok = set_plugin_enabled(plugin_id, enabled)
    print(f"[🧩 插件] set_plugin_enabled 结果: {ok}")
    
    # 动态注册路由（禁用时无法移除，但路由会检查插件启用状态）
    if ok and enabled:
        plugin = _registry.get(plugin_id)
        print(f"[🧩 插件] 获取插件实例: {plugin}, router={plugin.router.prefix if plugin else 'N/A'}, _app={_app is not None}")
        if plugin and _app:
            # 检查路由是否已注册（通过检查 prefix）
            route_prefix = plugin.router.prefix
            already_registered = any(
                hasattr(route, 'path') and route.path.startswith(route_prefix)
                for route in _app.routes
            )
            print(f"[🧩 插件] 路由已注册检查: {already_registered}, prefix={route_prefix}")
            if not already_registered:
                try:
                    _app.include_router(plugin.router)
                    print(f"[🧩 插件] ✅ 动态注册路由成功: {route_prefix}")
                except Exception as e:
                    print(f"[🧩 插件] ❌ 动态注册路由失败: {e}")
                    import traceback
                    traceback.print_exc()
            else:
                print(f"[🧩 插件] 路由已存在，跳过注册")
        elif not _app:
            print(f"[🧩 插件] ⚠️ _app 未设置，无法动态注册路由")
    
    return {"status": "success" if ok else "error"}


@router.get("/api/plugins/{plugin_id}/config")
def api_get_plugin_config(plugin_id: str, request: Request):
    # 🔒 安全检查：必须管理员
    if not user_service.is_admin_user(request):
        return {"status": "error", "message": "需要管理员权限"}
    plugin = get_plugin(plugin_id)
    if not plugin:
        return {"status": "error", "message": "插件不存在"}
    
    # Pro 插件权限检查
    if getattr(plugin, 'pro_only', False) and not is_pro_user():
        return {"status": "error", "message": "此插件需要 Pro 授权", "need_pro": True}
    
    schema = plugin.get_config_schema()
    values = get_plugin_config(plugin_id)
    
    print(f"[🧩 插件配置] plugin_id={plugin_id}, schema_count={len(schema)}, values_keys={list(values.keys())}")
    
    return {"status": "success", "data": {
        "schema": schema,
        "values": values
    }}


@router.post("/api/plugins/{plugin_id}/config")
async def api_save_plugin_config(plugin_id: str, request: Request):
    # 🔒 安全检查：必须管理员
    if not user_service.is_admin_user(request):
        return {"status": "error", "message": "需要管理员权限"}
    data = await request.json()
    # 支持两种格式：直接传配置对象 或 包裹在 config 字段中
    config_data = data.get("config", data) if isinstance(data, dict) else {}
    # 使用 update_plugin_config 合并配置，而不是覆盖
    update_plugin_config(plugin_id, config_data, merge=True)
    return {"status": "success"}


@router.get("/api/plugins/{plugin_id}/logs")
def api_get_plugin_logs(plugin_id: str, request: Request, limit: int = 50):
    """获取插件日志"""
    # 🔒 安全检查：必须管理员
    if not user_service.is_admin_user(request):
        return {"status": "error", "message": "需要管理员权限"}
    plugin = get_plugin(plugin_id)
    if not plugin:
        return {"status": "error", "message": "插件不存在"}
    logs = plugin.get_logs(limit)
    return {"status": "success", "data": logs}


@router.post("/api/plugins/{plugin_id}/logs/clear")
def api_clear_plugin_logs(plugin_id: str, request: Request):
    """清空插件日志"""
    # 🔒 安全检查：必须管理员
    if not user_service.is_admin_user(request):
        return {"status": "error", "message": "需要管理员权限"}
    plugin = get_plugin(plugin_id)
    if not plugin:
        return {"status": "error", "message": "插件不存在"}
    success = plugin.clear_logs()
    return {"status": "success" if success else "error"}


# ==========================================
# 通知模板插件专用 API
# ==========================================
@router.get("/api/plugins/notify_template/template/{template_key}")
def api_get_notify_template(template_key: str, request: Request, style: str = "default"):
    """获取指定风格的模板内容"""
    # 🔒 安全检查：必须管理员
    if not user_service.is_admin_user(request):
        return {"status": "error", "message": "需要管理员权限"}
    plugin = get_plugin("notify_template")
    if not plugin:
        return {"status": "error", "message": "插件不存在"}
    
    # 获取模板（不随机，返回第一条用于编辑）
    if style == "custom":
        config = get_plugin_config("notify_template")
        template = config.get(template_key, "")
    elif style == "default":
        from app.plugins.notify_template.plugin import DEFAULT_TEMPLATES
        template = DEFAULT_TEMPLATES.get(template_key, "")
    else:
        from app.plugins.notify_template.plugin import STYLE_TEMPLATES, DEFAULT_TEMPLATES
        style_tpls = STYLE_TEMPLATES.get(style, {}).get(template_key, [])
        template = style_tpls[0] if style_tpls else DEFAULT_TEMPLATES.get(template_key, "")
    
    return {"status": "success", "template": template}


@router.post("/api/plugins/notify_template/preview")
async def api_preview_notify_template(request: Request):
    """预览模板"""
    # 🔒 安全检查：必须管理员
    if not user_service.is_admin_user(request):
        return {"status": "error", "message": "需要管理员权限"}
    data = await request.json()
    template_key = data.get("template_key", "library_new_episode")
    template_text = data.get("template_text", "")
    
    plugin = get_plugin("notify_template")
    if not plugin:
        return {"status": "error", "message": "插件不存在"}
    
    preview = plugin.preview(template_key, template_text)
    return {"status": "success", "preview": preview}


# ==========================================
# 2.3 插件导入 / 本项目 GitHub 官方发布窗口
# ==========================================
# 所有导入接口都在 /api/plugins/ 下，AuthGate 已将其列为管理员专属前缀；
# 此处再做一次管理员判定作为纵深防御。

_REGISTRY_CACHE = {"at": 0.0, "data": None, "ref": ""}
_REGISTRY_TTL = 300  # 秒，避免频繁请求 GitHub


def _admin_ok(request: Request) -> bool:
    return bool(user_service.is_admin_user(request))


def _import_summary(mf: dict, origin: dict) -> dict:
    return {
        "id": mf["id"],
        "name": mf["name"],
        "version": mf["version"],
        "enabled": False,
        "requires_enable": True,
        "source": origin.get("source", "local"),
        "origin": origin,
        "message": "导入成功；插件默认禁用，需在插件中心显式启用后才会加载代码",
    }


@router.get("/api/plugins/registry")
def api_plugin_registry(request: Request, ref: str = ""):
    """读取本项目 GitHub 仓库 plugins/index.json（官方插件发布窗口）。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    from app.plugins import github_source

    now = time.time()
    if (_REGISTRY_CACHE["data"] is not None
            and _REGISTRY_CACHE["ref"] == ref
            and now - _REGISTRY_CACHE["at"] < _REGISTRY_TTL):
        return {"status": "success", "data": _REGISTRY_CACHE["data"], "cached": True}
    try:
        data = github_source.fetch_registry(ref=ref or None)
    except Exception as exc:
        return {"status": "error", "message": "读取官方索引失败: %s" % exc}
    _REGISTRY_CACHE.update({"at": now, "data": data, "ref": ref})
    return {"status": "success", "data": data, "cached": False}


@router.post("/api/plugins/import")
async def api_import_plugin(request: Request, file: UploadFile = File(...)):
    """上传 zip 包导入外部插件：只落盘、默认禁用、不执行插件代码。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    from app.plugins import external, plugin_dao

    data = await file.read(external.MAX_ZIP_BYTES + 1)
    if len(data) > external.MAX_ZIP_BYTES:
        return {"status": "error", "message": "插件包超过 %d 字节上限" % external.MAX_ZIP_BYTES}
    overwrite = request.query_params.get("overwrite") == "1"
    try:
        result = await run_in_threadpool(
            external.install_from_zip, data,
            {"type": "local"}, overwrite,
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    mf = result["manifest"]
    # 关键：无论历史上是否装过同 id 插件，导入后一律回到禁用状态
    plugin_dao.set_plugin_enabled(mf["id"], False)
    # 2.5 修复：同步内存 manifest 表，导入后插件中心立即可见（此前要重启才出现）
    from app.plugins import refresh_external_manifests
    refresh_external_manifests()
    return {"status": "success", "data": _import_summary(mf, result["origin"])}


@router.post("/api/plugins/import/github")
async def api_import_plugin_from_github(request: Request):
    """从本项目 GitHub 发布窗口拉取并安装；产物强制 sha256 校验。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    from app.plugins import github_source, plugin_dao

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        result = await run_in_threadpool(
            github_source.install_from_github,
            str(payload.get("plugin_id", "")),
            payload.get("ref") or None,
            github_source.DEFAULT_REPO,
            bool(payload.get("overwrite")),
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    mf = result["manifest"]
    plugin_dao.set_plugin_enabled(mf["id"], False)
    # 2.5 修复：同步内存 manifest 表，导入后插件中心立即可见
    from app.plugins import refresh_external_manifests
    refresh_external_manifests()
    return {"status": "success", "data": _import_summary(mf, result["origin"])}


@router.delete("/api/plugins/{plugin_id}/install")
def api_uninstall_plugin(plugin_id: str, request: Request):
    """卸载外部导入的插件；内置插件不可卸载。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    from app.plugins import uninstall_plugin, plugin_dao, refresh_external_manifests

    try:
        ok = uninstall_plugin(plugin_id)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    if not ok:
        return {"status": "error", "message": "仅可卸载外部导入的插件"}
    # 清理启用状态，避免重新导入同 id 时被当作「已启用」自动执行代码。
    # 磁盘目录已删除，后续清理失败不得把「卸载成功」翻转成 500（2.5.1 修复）。
    try:
        plugin_dao.delete_plugin_state(plugin_id)
    except Exception:
        return {"status": "success", "message": "已卸载（启用状态清理失败，不影响卸载结果）"}
    try:
        refresh_external_manifests()
    except Exception:
        pass
    return {"status": "success", "message": "已卸载；重启应用后残留路由将彻底清理"}


# ==========================================
# 2.5 开发资源：开发指南 / AI 开发提示词 / 示例插件
# ==========================================
# 内容以 docs/ 为唯一事实源（Docker 镜像通过 COPY docs/ 带入）；
# 示例在内存中即时生成，保证与当前版本插件契约一致。

_DEV_DOC_MAX_BYTES = 512 * 1024


def _doc_response(content: str) -> dict:
    if not content:
        return {"status": "error", "message": "文档资源缺失（docs/ 未随应用部署）"}
    return {"status": "success", "content": content[:_DEV_DOC_MAX_BYTES]}


@router.get("/api/plugins/dev/ai-prompt")
def api_plugin_ai_prompt(request: Request):
    """AI 开发提示词全文（复制给任意 AI 生成插件包）。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    from app.plugins import dev_resources
    return _doc_response(dev_resources.get_ai_prompt())


@router.get("/api/plugins/dev/guide")
def api_plugin_dev_guide(request: Request):
    """插件开发指南全文。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    from app.plugins import dev_resources
    return _doc_response(dev_resources.get_dev_guide())


@router.get("/api/plugins/dev/example-zip")
def api_plugin_example_zip(request: Request):
    """下载 ZIP 开发示例（内存即时生成，通过 2.3 全部安全校验）。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    from fastapi.responses import Response
    from app.plugins import dev_resources

    data = dev_resources.build_example_zip()
    filename = "hello_plugin-%s.zip" % dev_resources.EXAMPLE_PLUGIN_VERSION
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="%s"' % filename},
    )


@router.get("/api/plugins/dev/example-single")
def api_plugin_example_single(request: Request):
    """下载单文件 .py 开发示例。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    from fastapi.responses import Response
    from app.plugins import dev_resources

    return Response(
        content=dev_resources.EXAMPLE_PLUGIN_PY,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="hello_single.py"'},
    )


# ==========================================
# 2.5 定时执行：插件全局调度（间隔存于插件配置 schedule_minutes，0 = 关）
# ==========================================

_SCHEDULE_MIN, _SCHEDULE_MAX = 0, 7 * 24 * 60  # 0=关闭；上限 7 天


def _schedule_minutes_of(plugin_id: str) -> int:
    from app.plugins import get_plugin_config

    try:
        minutes = int(get_plugin_config(plugin_id).get("schedule_minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    return max(0, min(minutes, _SCHEDULE_MAX))


@router.get("/api/plugins/schedules")
def api_plugin_schedules(request: Request):
    """所有插件的定时执行配置与状态（供「定时执行」面板渲染）。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    from app.plugins import get_all_plugins
    from app.plugins.scheduler import get_plugin_scheduler

    scheduler = get_plugin_scheduler()
    items = []
    for meta in get_all_plugins():
        minutes = _schedule_minutes_of(meta["id"])
        last_run = scheduler.last_run_of(meta["id"]) if scheduler else None
        items.append({
            "id": meta["id"],
            "name": meta["name"],
            "enabled": meta["enabled"],
            "loaded": meta.get("loaded", True),
            "external": meta.get("external", False),
            "minutes": minutes,
            "last_run": last_run,
            "next_due_in": scheduler.next_due_in(meta["id"], minutes) if scheduler else None,
        })
    return {"status": "success", "data": items}


@router.post("/api/plugins/{plugin_id}/schedule")
async def api_set_plugin_schedule(plugin_id: str, request: Request):
    """设置插件定时执行间隔（分钟，0 = 关闭）；未启用的外部插件也允许预配置。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    from app.plugins import update_plugin_config

    try:
        data = await request.json()
    except Exception:
        data = {}
    minutes = data.get("minutes") if isinstance(data, dict) else None
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return {"status": "error", "message": "minutes 必须是整数分钟"}
    if minutes < _SCHEDULE_MIN or minutes > _SCHEDULE_MAX:
        return {"status": "error", "message": "minutes 取值 0（关闭）~ %d" % _SCHEDULE_MAX}
    update_plugin_config(plugin_id, {"schedule_minutes": minutes}, merge=True)
    return {"status": "success", "minutes": minutes}


@router.post("/api/plugins/{plugin_id}/run-schedule")
async def api_run_plugin_schedule(plugin_id: str, request: Request):
    """手动「立即执行」一次插件定时动作；仅对已加载（启用）的插件生效。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    from app.plugins import get_plugin
    from app.plugins.scheduler import get_plugin_scheduler

    plugin = get_plugin(plugin_id)
    if plugin is None or not plugin.enabled:
        return {"status": "error", "message": "插件未启用，定时动作不可执行"}
    if not hasattr(type(plugin), "on_schedule"):
        return {"status": "error", "message": "该插件未实现定时动作"}

    def _run():
        try:
            plugin.on_schedule()
            return True
        except Exception:
            return False

    ok = await run_in_threadpool(_run)
    scheduler = get_plugin_scheduler()
    if scheduler:
        scheduler.mark_ran(plugin_id)
    if not ok:
        return {"status": "error", "message": "执行失败，详见插件日志"}
    return {"status": "success", "message": "已触发一次定时动作"}
