"""入库通知两个 service 的公共实现（2.0 批次 B1）。

背景：`notification_bot_library_new_item_service.py` 与
`notification_bot_library_new_episode_service.py` 早期逐行同构，各自复制了
provider 装配样板、简介格式化、BaseURL 归一化、渠道折算等纯逻辑。

等价性契约（不得打破）：
1. 两个 service **仍各自持有独立的模块级 provider 变量**。运行期
   `bot_service.py` 会分别调用两者的 `set_dependency_providers(...)`，
   共享全局状态会改变注入语义，因此本模块只提供「作用于调用方命名空间」的
   装配函数，不持有任何 provider 状态。
2. 需要读取 provider 的公共函数一律**显式接收 provider 作为参数**，由 service
   侧的薄包装传入自己的模块级 provider，行为与旧实现逐字等价。
3. 对外可见的函数名、签名、返回值、模块级变量名一律不变，本模块纯加法。
"""
from __future__ import annotations

# 注入参数名 -> 模块级变量名。与旧版 set_dependency_providers 中的
# `global _xxx` + `if xxx is not None: _xxx = xxx` 一一对应。
_PROVIDER_ATTRS = {
    "enable_library_notify_provider": "_enable_library_notify_provider",
    "media_quality_info_provider": "_media_quality_info_provider",
    "media_server_main_public_or_host_provider": "_media_server_main_public_or_host_provider",
    "media_server_host_provider": "_media_server_host_provider",
    "notify_channels_provider": "_notify_channels_provider",
    "get_plugin_provider": "_get_plugin_provider",
    "report_cover_url_provider": "_report_cover_url_provider",
    "datetime_provider": "_datetime_provider",
    "re_provider": "_re_provider",
    "logger_provider": "_logger_provider",
    "bark_push_provider": "_bark_push_provider",
}


def apply_dependency_providers(namespace, **providers):
    """把非 None 的 provider 写入调用方模块命名空间。

    `namespace` 传调用方的 `globals()`；只覆盖显式传入且非 None 的项，
    与旧版逐条 `global` + `if ... is not None` 赋值完全等价。
    未知参数名直接忽略，保持旧版对额外关键字参数的宽松行为。
    """
    for arg_name, value in providers.items():
        if value is None:
            continue
        attr = _PROVIDER_ATTRS.get(arg_name)
        if attr is not None:
            namespace[attr] = value


def default_get_plugin(plugin_name):
    from app.plugins import get_plugin

    return get_plugin(plugin_name)


def default_bark_push_provider():
    """系统级 Bark 推送入口：惰性导入，避免模块加载期引入额外依赖。"""
    from app.domains.notifications.bark_push import safe_push

    return safe_push


def format_overview(info, re_provider):
    overview = str(info.get("Overview") or "")
    overview = re_provider.sub(r"<[^>]+>", "", overview).strip()
    if not overview:
        overview = "暂无简介..."
    if len(overview) > 150:
        overview = overview[:140] + "..."
    return overview


def format_type(type_raw):
    type_cn = "电影"
    type_icon = "🎬"
    if type_raw in ["Series", "Episode"]:
        type_cn = "剧集"
        type_icon = "📺"
    return type_cn, type_icon


def get_base_url(main_public_or_host_fn, host_fn):
    base_url = main_public_or_host_fn() or host_fn()
    if base_url and not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    return base_url


def get_platform(channels):
    return (
        "all"
        if "tg_bot" in channels and "wecom" in channels
        else "tg"
        if "tg_bot" in channels
        else "wecom"
        if "wecom" in channels
        else "none"
    )
