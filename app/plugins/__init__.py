"""
EmbyPulse 插件加载器
启动时扫描 app/plugins/ 下所有子目录，自动发现并注册插件
"""
import os
import importlib
import logging
from typing import Dict, List, Optional
from app.plugins.base import PluginBase, _config_cache, _config_cache_lock
from app.plugins import plugin_dao
from app.plugins import external

logger = logging.getLogger("uvicorn")

# 全局插件注册表
_registry: Dict[str, PluginBase] = {}
# 已安装的外部插件 manifest（2.3）：仅登记元信息，未启用前不执行任何插件代码
_external_manifests: Dict[str, dict] = {}


def _ensure_plugin_table():
    try:
        plugin_dao.ensure_plugin_tables()
        print("[🧩 插件] 数据库表初始化成功")
    except Exception as e:
        print(f"[🧩 插件] 初始化数据库表失败: {e}")
        import traceback
        traceback.print_exc()


def discover_plugins():
    """扫描 app/plugins/ 下所有含 plugin.py 的子目录"""
    _ensure_plugin_table()
    plugins_dir = os.path.dirname(__file__)

    for name in os.listdir(plugins_dir):
        pkg_dir = os.path.join(plugins_dir, name)
        if not os.path.isdir(pkg_dir):
            continue
        # 兼容源码(.py)和编译后(.pyc)两种模式
        has_plugin = os.path.exists(os.path.join(pkg_dir, "plugin.py")) or os.path.exists(os.path.join(pkg_dir, "plugin.pyc"))
        if not has_plugin:
            continue

        try:
            module = importlib.import_module(f"app.plugins.{name}.plugin")
            
            # 优先查找模块级别的 plugin 实例（推荐模式：模块底部创建实例并注册路由）
            if hasattr(module, 'plugin') and isinstance(module.plugin, PluginBase):
                instance = module.plugin
                if instance.id:
                    _registry[instance.id] = instance
                    logger.info(f"🧩 发现插件: {instance.name} (v{instance.version}), routes={len(instance.router.routes)}")
                continue
            
            # 回退：查找类并创建实例
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, PluginBase) and attr is not PluginBase:
                    instance = attr()
                    if instance.id:
                        _registry[instance.id] = instance
                        logger.info(f"🧩 发现插件: {instance.name} (v{instance.version})")
        except Exception as e:
            logger.error(f"加载插件 {name} 失败: {e}")
            import traceback
            traceback.print_exc()

    # 2.3：登记外部插件；只有数据库中已启用的才会真正 import 插件代码
    discover_external_plugins()

    # 从数据库恢复启用状态
    _restore_states()


def discover_external_plugins():
    """扫描持久化外部插件目录。

    安全约束：导入包只落盘并登记 manifest，**绝不在此处执行外部代码**；
    仅当管理员此前已显式启用时，才按需加载并交给 _restore_states() 启用。
    """
    global _external_manifests
    try:
        _external_manifests = external.list_installed()
    except Exception as e:
        logger.error("[🧩 插件] 扫描外部插件目录失败: %s", e)
        _external_manifests = {}
        return
    if not _external_manifests:
        return
    try:
        enabled = {row["plugin_id"] for row in plugin_dao.list_plugin_states() if row["enabled"]}
    except Exception:
        enabled = set()
    for pid in _external_manifests:
        if pid not in enabled:
            continue
        try:
            instance = external.load_external(pid)
            instance._external = True
            _registry[pid] = instance
            logger.info("🧩 载入外部插件: %s", pid)
        except Exception as e:
            logger.error("[🧩 插件] 载入外部插件 %s 失败: %s", pid, e)


def _restore_states():
    try:
        rows = plugin_dao.list_plugin_states()
        print(f"[🧩 插件] 从数据库恢复状态，发现 {len(rows)} 条记录")
        for row in rows:
            pid = row["plugin_id"]
            enabled = row["enabled"]
            config_json = row["config"]
            print(f"[🧩 插件] 检查插件 {pid}: enabled={enabled}, in_registry={pid in _registry}")
            
            # 🔥 加载配置到缓存
            try:
                import json

                config = json.loads(config_json) if config_json else {}
                with _config_cache_lock:
                    _config_cache[pid] = config
            except:
                pass
            
            if pid in _registry and enabled:
                print(f"[🧩 插件] 启用插件: {pid}")
                _registry[pid].enable()
    except Exception as e:
        print(f"[🧩 插件] 恢复状态异常: {e}")
        import traceback
        traceback.print_exc()


def get_all_plugins() -> List[dict]:
    """内置 + 已安装的外部插件（未启用的外部插件也列出，便于管理员启用/卸载）。"""
    result = []
    for p in _registry.values():
        meta = p.get_meta()
        is_external = bool(getattr(p, "_external", False))
        meta.update({
            "external": is_external,
            "installed": True,
            "loaded": True,
            "source": external.get_origin(p.id).get("source", "local") if is_external else "builtin",
            "origin": external.get_origin(p.id) if is_external else {},
        })
        result.append(meta)
    known = {m["id"] for m in result}
    for pid, mf in _external_manifests.items():
        if pid not in known:
            result.append(external.manifest_meta(mf, enabled=False))
    return result


def get_external_manifests() -> Dict[str, dict]:
    return dict(_external_manifests)


def uninstall_plugin(plugin_id: str) -> bool:
    """卸载外部插件（内置插件永不受影响）；已启用的先停止。"""
    plugin = _registry.get(plugin_id)
    if plugin is not None and getattr(plugin, "_external", False):
        try:
            plugin.disable()
        except Exception as e:
            logger.error("停止外部插件 %s 失败: %s", plugin_id, e)
        _registry.pop(plugin_id, None)
    if plugin_id not in _external_manifests:
        return False
    try:
        ok = external.uninstall(plugin_id)
    except Exception as e:
        logger.error("卸载外部插件 %s 失败: %s", plugin_id, e)
        return False
    if ok:
        _external_manifests.pop(plugin_id, None)
    return ok


def get_plugin(plugin_id: str) -> Optional[PluginBase]:
    return _registry.get(plugin_id)


def get_enabled_plugins() -> List[PluginBase]:
    return [p for p in _registry.values() if p.enabled]


def disable_enabled_plugins() -> None:
    """Disable currently enabled plugin instances without changing persisted state."""
    for plugin in list(get_enabled_plugins()):
        try:
            plugin.disable()
        except Exception as e:
            logger.error(f"停止插件 {plugin.id} 失败: {e}")


def set_plugin_enabled(plugin_id: str, enabled: bool) -> bool:
    plugin = _registry.get(plugin_id)
    # 2.3：外部插件首次被显式启用时才 import 其代码（导入阶段不执行）
    if plugin is None and enabled and plugin_id in _external_manifests:
        try:
            plugin = external.load_external(plugin_id)
            plugin._external = True
            _registry[plugin_id] = plugin
            logger.info("🧩 按需载入外部插件: %s", plugin_id)
        except Exception as e:
            logger.error("[🧩 插件] 按需载入外部插件 %s 失败: %s", plugin_id, e)
            return False
    if not plugin:
        return False
    if enabled:
        plugin.enable()
    else:
        plugin.disable()
    try:
        plugin_dao.set_plugin_enabled(plugin_id, enabled)
    except Exception:
        pass
    return True


def get_plugin_config(plugin_id: str) -> dict:
    try:
        return plugin_dao.get_plugin_config(plugin_id)
    except Exception:
        return {}


def save_plugin_config(plugin_id: str, config: dict):
    """保存插件配置并刷新缓存"""
    global _config_cache
    try:
        plugin_dao.save_plugin_config(plugin_id, config)
        # 刷新缓存
        with _config_cache_lock:
            _config_cache[plugin_id] = config
    except Exception:
        pass


def update_plugin_config(plugin_id: str, updates: dict, merge: bool = True):
    """更新插件配置
    
    Args:
        plugin_id: 插件ID
        updates: 配置更新
        merge: 是否合并现有配置（默认True）。如果为False，直接保存updates
    """
    if merge:
        current = get_plugin_config(plugin_id)
        current.update(updates)
        save_plugin_config(plugin_id, current)
        return current
    else:
        save_plugin_config(plugin_id, updates)
        return updates


def get_plugin_logs(plugin_id: str, limit: int = 50):
    """获取插件日志"""
    plugin = _registry.get(plugin_id)
    if plugin:
        return plugin.get_logs(limit)
    return []


def clear_plugin_logs(plugin_id: str):
    """清空插件日志"""
    plugin = _registry.get(plugin_id)
    if plugin:
        return plugin.clear_logs()
    return False
