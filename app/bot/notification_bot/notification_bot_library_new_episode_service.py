import datetime
import logging
import re
from collections import defaultdict

from app.core.config import REPORT_COVER_URL
from app.infra.config.media_server_settings import (
    get_media_server_host,
    get_media_server_main_public_or_host,
)
from app.infra.config.notification_settings import get_enable_library_notify
from app.bot.notification_bot import _library_common


logger = logging.getLogger("uvicorn")


def _default_get_plugin(plugin_name):
    from app.plugins import get_plugin

    return get_plugin(plugin_name)


_enable_library_notify_provider = lambda: get_enable_library_notify
_media_quality_info_provider = lambda: (lambda _item_id: {})
_media_server_main_public_or_host_provider = lambda: get_media_server_main_public_or_host
_media_server_host_provider = lambda: get_media_server_host
_notify_channels_provider = lambda: (lambda _notify_type: [])
_get_plugin_provider = lambda: _default_get_plugin
_report_cover_url_provider = lambda: REPORT_COVER_URL
_datetime_provider = lambda: datetime
_re_provider = lambda: re
_logger_provider = lambda: logger


def _default_bark_push_provider():
    """系统级 Bark 推送入口（1.9.0）：惰性导入，避免模块加载期引入额外依赖。"""
    from app.domains.notifications.bark_push import safe_push

    return safe_push


_bark_push_provider = _default_bark_push_provider


def set_dependency_providers(
    *,
    enable_library_notify_provider=None,
    media_quality_info_provider=None,
    media_server_main_public_or_host_provider=None,
    media_server_host_provider=None,
    notify_channels_provider=None,
    get_plugin_provider=None,
    report_cover_url_provider=None,
    datetime_provider=None,
    re_provider=None,
    logger_provider=None,
    bark_push_provider=None,
):
    """2.0 B1：装配样板收敛到 _library_common，签名与逐项覆盖语义不变。"""
    _library_common.apply_dependency_providers(
        globals(),
        enable_library_notify_provider=enable_library_notify_provider,
        media_quality_info_provider=media_quality_info_provider,
        media_server_main_public_or_host_provider=media_server_main_public_or_host_provider,
        media_server_host_provider=media_server_host_provider,
        notify_channels_provider=notify_channels_provider,
        get_plugin_provider=get_plugin_provider,
        report_cover_url_provider=report_cover_url_provider,
        datetime_provider=datetime_provider,
        re_provider=re_provider,
        logger_provider=logger_provider,
        bark_push_provider=bark_push_provider,
    )

def _format_episode_suffix(episodes):
    season_groups = defaultdict(list)
    for ep in episodes:
        season_groups[ep.get("ParentIndexNumber", 1)].append(ep)

    season_strs = []
    total_eps = 0

    for s_idx in sorted(season_groups.keys()):
        s_eps = season_groups[s_idx]
        ep_indices = sorted(list(set([e.get("IndexNumber", 0) for e in s_eps if e.get("IndexNumber") is not None])))
        total_eps += len(ep_indices)
        if len(ep_indices) > 1:
            ranges = []
            start = ep_indices[0]
            end = ep_indices[0]
            for idx in ep_indices[1:]:
                if idx == end + 1:
                    end = idx
                else:
                    ranges.append(f"E{str(start).zfill(2)}" if start == end else f"E{str(start).zfill(2)}-E{str(end).zfill(2)}")
                    start = idx
                    end = idx
            ranges.append(f"E{str(start).zfill(2)}" if start == end else f"E{str(start).zfill(2)}-E{str(end).zfill(2)}")
            season_strs.append(f"S{str(s_idx).zfill(2)}{', '.join(ranges)}")
        elif len(ep_indices) == 1:
            season_strs.append(f"S{str(s_idx).zfill(2)}E{str(ep_indices[0]).zfill(2)}")

    final_ep_str = ", ".join(season_strs)
    title_suffix = f"{final_ep_str} (共{total_eps}集)" if total_eps > 1 else final_ep_str

    if total_eps == 1 and len(episodes) == 1:
        ep_name = episodes[0].get("Name", "")
        if ep_name and "Episode" not in ep_name and "第" not in ep_name:
            title_suffix += f" {ep_name}"

    return title_suffix


def _format_overview(series_info):
    return _library_common.format_overview(series_info, _re_provider())

def _get_quality_info(episodes):
    quality_info = {"quality": "", "video_codec": "", "audio_codec": "", "resolution": "", "hdr": "", "quality_icon": ""}
    if episodes:
        ep_id = episodes[0].get("Id", "")
        _logger_provider().info(f"[媒体质量] 准备获取剧集质量信息: ep_id={ep_id}")
        quality_info = _media_quality_info_provider()(ep_id)
        _logger_provider().info(f"[媒体质量] 获取结果: {quality_info}")
    return quality_info


def _get_base_url():
    return _library_common.get_base_url(
        _media_server_main_public_or_host_provider(),
        _media_server_host_provider(),
    )

def _render_caption(tpl_vars, series_name, title_suffix, year, rating, overview):
    try:
        tpl_plugin = _get_plugin_provider()("notify_template")
        if tpl_plugin and tpl_plugin.enabled:
            return tpl_plugin.render("library_new_episode", tpl_vars)
        raise Exception("fallback")
    except:
        return (
            f"📺 <b>新入库 剧集 {series_name}</b> {title_suffix}\n\n📌 年份：{year}  |  ⭐ 评分：{rating}\n"
            f"🕒 时间：{_datetime_provider().datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"📝 <b>剧情简介：</b>\n{overview}"
        )


def _get_platform(channels):
    return _library_common.get_platform(channels)

def _bark_notify_new_episode(data):
    """系统级 Bark 剧集入库通知：由 Bark 自身开关控制，失败绝不影响主流程。

    正文复用「自定义通知模板」插件的 library_new_episode 模板，与 TG / 企业微信
    风格一致（通过 push_new_library_item 的 body 覆盖实现）。
    """
    try:
        info = data.get("series_info") or {}
        series_id = data.get("series_id", "")
        title_suffix = _format_episode_suffix(data.get("episodes") or [])
        series_name = info.get("Name", "未知剧集")
        year = info.get("ProductionYear", "")
        rating = info.get("CommunityRating", "N/A")
        overview = _format_overview(info)
        quality_info = _get_quality_info(data.get("episodes") or [])

        tpl_vars = {
            "series_name": series_name,
            "episode_info": title_suffix,
            "year": year,
            "rating": rating,
            "time": _datetime_provider().datetime.now().strftime("%Y-%m-%d %H:%M"),
            "overview": overview,
            "quality": quality_info.get("quality", ""),
            "quality_icon": quality_info.get("quality_icon", "📺"),
            "video_codec": quality_info.get("video_codec", ""),
            "audio_codec": quality_info.get("audio_codec", ""),
            "resolution": quality_info.get("resolution", ""),
            "hdr": quality_info.get("hdr", ""),
        }
        body = _render_caption(tpl_vars, series_name, title_suffix, year, rating, overview)
        res = _bark_push_provider()(
            "library_new",
            body=body,
            name=str(info.get("Name") or ""),
            series_name=str(info.get("Name") or ""),
            episode_info=title_suffix,
            item_type="Series",
            year=info.get("ProductionYear", ""),
            rating=info.get("CommunityRating", ""),
            overview=_format_overview(info),
        )
        if res:
            if res.get("skipped"):
                _logger_provider().info(f"[Bark] 新片入库未推送(跳过): {res.get('detail')}")
            else:
                _logger_provider().info(f"[Bark] 新片入库通知: {res.get('detail')}")
    except Exception as e:
        _logger_provider().warning(f"[Bark] 新片入库通知异常: {e}")


def handle_library_new_episode(bot, data):
    _info = data.get("series_info") or {}
    _logger_provider().info(f"[入库通知] 收到新剧集入库事件: {_info.get('Name') or data.get('series_id')} ({len(data.get('episodes') or [])} 集)")
    # 系统级 Bark 通道独立于「入库通知总开关」，仅由自身开关控制
    _bark_notify_new_episode(data)

    # 用户级通知：新剧集入库后，向开启「新片入库」开关的用户广播（正文复用模板，不含网址）
    try:
        info = data.get("series_info") or {}
        title_suffix = _format_episode_suffix(data.get("episodes") or [])
        series_name = info.get("Name", "未知剧集")
        year = info.get("ProductionYear", "")
        rating = info.get("CommunityRating", "N/A")
        overview = _format_overview(info)
        quality_info = _get_quality_info(data.get("episodes") or [])
        _tpl_vars = {
            "series_name": series_name,
            "episode_info": title_suffix,
            "year": year,
            "rating": rating,
            "time": _datetime_provider().datetime.now().strftime("%Y-%m-%d %H:%M"),
            "overview": overview,
            "quality": quality_info.get("quality", ""),
            "quality_icon": quality_info.get("quality_icon", "📺"),
            "video_codec": quality_info.get("video_codec", ""),
            "audio_codec": quality_info.get("audio_codec", ""),
            "resolution": quality_info.get("resolution", ""),
            "hdr": quality_info.get("hdr", ""),
        }
        _body = _render_caption(_tpl_vars, series_name, title_suffix, year, rating, overview)
        from app.domains.users import user_notify_service as _uns
        _uns.notify_users_by_flag("notify_new_item", "新片入库", _body)
    except Exception as _e:
        _logger_provider().warning(f"[用户通知] 新剧集入库广播推送失败: {_e}")

    if not _enable_library_notify_provider()():
        return

    series_id = data["series_id"]
    episodes = data["episodes"]
    series_info = data["series_info"]

    title_suffix = _format_episode_suffix(episodes)
    series_name = series_info.get("Name", "未知剧集")
    year = series_info.get("ProductionYear", "")
    rating = series_info.get("CommunityRating", "N/A")
    overview = _format_overview(series_info)
    quality_info = _get_quality_info(episodes)

    base_url = _get_base_url()
    play_url = f"{base_url}/web/index.html#!/item?id={series_id}&serverId={series_info.get('ServerId','')}"

    tpl_vars = {
        "series_name": series_name,
        "episode_info": title_suffix,
        "year": year,
        "rating": rating,
        "time": _datetime_provider().datetime.now().strftime("%Y-%m-%d %H:%M"),
        "overview": overview,
        "quality": quality_info.get("quality", ""),
        "quality_icon": quality_info.get("quality_icon", "📺"),
        "video_codec": quality_info.get("video_codec", ""),
        "audio_codec": quality_info.get("audio_codec", ""),
        "resolution": quality_info.get("resolution", ""),
        "hdr": quality_info.get("hdr", ""),
    }
    caption = _render_caption(tpl_vars, series_name, title_suffix, year, rating, overview)

    keyboard = None
    if base_url and base_url.startswith(("http://", "https://")):
        keyboard = {"inline_keyboard": [[{"text": "▶️ 立即播放", "url": play_url}]]}

    primary_io = bot._download_emby_image(series_id, "Primary")
    backdrop_io = bot._download_emby_image(series_id, "Backdrop")
    tg_img = backdrop_io or primary_io or _report_cover_url_provider()
    wecom_img = backdrop_io or primary_io or _report_cover_url_provider()

    channels = _notify_channels_provider()("library_new")
    platform = _get_platform(channels)

    if platform != "none":
        bot.send_photo(
            "sys_notify",
            tg_img,
            caption,
            reply_markup=keyboard,
            platform=platform,
            wecom_photo_io=wecom_img,
        )

    if "tg_channel" in channels:
        bot._notify_channels(tg_img, caption, keyboard, "episode", series_info)
