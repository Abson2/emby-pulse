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
    return {"status": "success", "data": _import_summary(mf, result["origin"])}


@router.delete("/api/plugins/{plugin_id}/install")
def api_uninstall_plugin(plugin_id: str, request: Request):
    """卸载外部导入的插件；内置插件不可卸载。"""
    if not _admin_ok(request):
        return {"status": "error", "message": "需要管理员权限"}
    from app.plugins import uninstall_plugin, plugin_dao

    try:
        ok = uninstall_plugin(plugin_id)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
    if not ok:
        return {"status": "error", "message": "仅可卸载外部导入的插件"}
    # 清理启用状态，避免重新导入同 id 时被当作「已启用」自动执行代码
    plugin_dao.delete_plugin_state(plugin_id)
    return {"status": "success", "message": "已卸载；重启应用后残留路由将彻底清理"}
