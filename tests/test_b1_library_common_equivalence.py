#!/usr/bin/env python3
"""2.0 批次 B1 等价性回归（纯标准库，可直接 python 运行）。

锁定「双胞胎入库通知 service 抽公共逻辑」前后行为完全一致：
  1. 两个 service 仍各自持有独立 provider 状态，互不串扰；
  2. set_dependency_providers 的签名、逐项覆盖、None 不覆盖语义不变；
  3. _format_overview / _get_base_url / _get_platform / _format_type 与旧实现
     逐字复制的 oracle 在多组输入下输出一致；
  4. 后半段 handler / Bark 通知 / 用户级广播入口全部保留。

用法: python tests/test_b1_library_common_equivalence.py [ROOT]
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import re
import sys
import types

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                       else os.path.join(os.path.dirname(__file__), ".."))
PASSED = 0
FAILS = []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  " + name)
    else:
        FAILS.append(name + ("  " + detail if detail else ""))
        print("  FAIL  " + name + ("  " + detail if detail else ""))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def stub(name, **attrs):
    m = types.ModuleType(name)
    m.__path__ = []
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---- 桩掉 FastAPI 依赖链，让 service 模块可在纯标准库环境加载 ----
stub("app")
stub("app.core")
stub("app.core.config", REPORT_COVER_URL="https://example.com/cover")
stub("app.infra")
stub("app.infra.config")
stub("app.infra.config.media_server_settings",
     get_media_server_host=lambda: "emby.example.com",
     get_media_server_main_public_or_host=lambda: "")
stub("app.infra.config.notification_settings",
     get_enable_library_notify=lambda *a, **k: True)
stub("app.bot")
pkg = stub("app.bot.notification_bot")

BASE = "app/bot/notification_bot/"
common = load("app.bot.notification_bot._library_common", BASE + "_library_common.py")
pkg._library_common = common
item_mod = load("app.bot.notification_bot.notification_bot_library_new_item_service",
                BASE + "notification_bot_library_new_item_service.py")
ep_mod = load("app.bot.notification_bot.notification_bot_library_new_episode_service",
              BASE + "notification_bot_library_new_episode_service.py")

# ---- 旧实现 oracle（从抽离前源码逐字复制，仅把 provider 改为参数） ----
def old_format_overview(info, re_provider):
    overview = str(info.get("Overview") or "")
    overview = re_provider.sub(r"<[^>]+>", "", overview).strip()
    if not overview:
        overview = "暂无简介..."
    if len(overview) > 150:
        overview = overview[:140] + "..."
    return overview


def old_get_base_url(main_provider, host_provider):
    base_url = main_provider()() or host_provider()()
    if base_url and not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    return base_url


def old_get_platform(channels):
    return (
        "all" if "tg_bot" in channels and "wecom" in channels
        else "tg" if "tg_bot" in channels
        else "wecom" if "wecom" in channels
        else "none"
    )


def old_format_type(type_raw):
    type_cn, type_icon = "电影", "🎬"
    if type_raw in ["Series", "Episode"]:
        type_cn, type_icon = "剧集", "📺"
    return type_cn, type_icon


EXPECTED_PARAMS = [
    "enable_library_notify_provider", "media_quality_info_provider",
    "media_server_main_public_or_host_provider", "media_server_host_provider",
    "notify_channels_provider", "get_plugin_provider",
    "report_cover_url_provider", "datetime_provider", "re_provider",
    "logger_provider", "bark_push_provider",
]


def old_apply(namespace, **providers):
    """旧版 set_dependency_providers 的逐条 if 赋值语义。"""
    attrs = dict(zip(EXPECTED_PARAMS, [
        "_enable_library_notify_provider", "_media_quality_info_provider",
        "_media_server_main_public_or_host_provider", "_media_server_host_provider",
        "_notify_channels_provider", "_get_plugin_provider",
        "_report_cover_url_provider", "_datetime_provider", "_re_provider",
        "_logger_provider", "_bark_push_provider",
    ]))
    for arg_name, value in providers.items():
        if value is not None and arg_name in attrs:
            namespace[attrs[arg_name]] = value


print("== 1. 模块与对外符号完整 ==")
for label, mod, handler in (
    ("new_item", item_mod, "handle_library_new_item"),
    ("new_episode", ep_mod, "handle_library_new_episode"),
):
    check(f"{label} 公共符号未丢失",
          all(hasattr(mod, n) for n in (
              "set_dependency_providers", handler, "_format_overview",
              "_get_base_url", "_get_platform", "_bark_notify_"
              + ("new_item" if label == "new_item" else "new_episode"))))
    sig = inspect.signature(mod.set_dependency_providers)
    params = list(sig.parameters)
    check(f"{label} 注入签名保持 11 个 keyword-only 参数且顺序不变",
          params == EXPECTED_PARAMS
          and all(sig.parameters[p].kind == inspect.Parameter.KEYWORD_ONLY
                  for p in params),
          repr(params))

print("== 2. 装配语义与状态隔离 ==")
sentinel_a, sentinel_b = object(), object()
old_ns = {}
new_ns = {}
kwargs = {"logger_provider": sentinel_a, "re_provider": re,
          "bark_push_provider": None, "datetime_provider": None}
old_apply(old_ns, **kwargs)
common.apply_dependency_providers(new_ns, **kwargs)
check("apply_dependency_providers 与旧逐条赋值结果一致", old_ns == new_ns,
      repr((old_ns, new_ns)))
check("None 参数不覆盖已有值",
      "_bark_push_provider" not in new_ns and "_datetime_provider" not in new_ns)

item_mod.set_dependency_providers(logger_provider=sentinel_a)
check("注入 new_item 后 new_episode 的 provider 不受影响",
      ep_mod._logger_provider is not sentinel_a)
ep_mod.set_dependency_providers(logger_provider=sentinel_b)
check("两个 service 各自持有独立 provider 状态",
      item_mod._logger_provider is sentinel_a
      and ep_mod._logger_provider is sentinel_b)
item_mod.set_dependency_providers(logger_provider=None)
check("后续 None 不覆盖已注入值", item_mod._logger_provider is sentinel_a)

print("== 3. 纯函数与旧实现逐字等价 ==")
overview_cases = [
    {}, {"Overview": None}, {"Overview": "<p>简短剧情</p>"},
    {"Overview": "  <b>带空格</b>  "}, {"Overview": "长" * 200},
    {"Overview": "A & B <script>x</script>"},
]
for label, mod in (("new_item", item_mod), ("new_episode", ep_mod)):
    for case in overview_cases:
        check(f"{label}._format_overview 等价 {list(case.values())[:1]}",
              mod._format_overview(case) == old_format_overview(case, re))

for main_val, host_val in (
    ("", "emby.example.com"), ("emby.example.com", "ignored"),
    ("https://emby.example.com", "ignored"), ("http://emby.example.com", "ignored"),
    (None, ""),
):
    for label, mod in (("new_item", item_mod), ("new_episode", ep_mod)):
        main_provider = lambda v=main_val: (lambda: v)
        host_provider = lambda v=host_val: (lambda: v)
        mod._media_server_main_public_or_host_provider = main_provider
        mod._media_server_host_provider = host_provider
        expected = old_get_base_url(main_provider, host_provider)
        got = mod._get_base_url()
        check(f"{label}._get_base_url 等价 main={main_val!r} host={host_val!r}",
              got == expected, f"{got!r} != {expected!r}")

for channels in ([], ["tg_bot"], ["wecom"], ["tg_bot", "wecom"],
                 ["bark"], ["tg_bot", "bark", "wecom"]):
    for label, mod in (("new_item", item_mod), ("new_episode", ep_mod)):
        check(f"{label}._get_platform 等价 {channels}",
              mod._get_platform(channels) == old_get_platform(channels))

for type_raw in ("Movie", "Series", "Episode", None, "movie"):
    check(f"_format_type 等价 {type_raw!r}",
          item_mod._format_type(type_raw) == old_format_type(type_raw))
check("公共模块 format_type 与旧实现等价",
      all(common.format_type(t) == old_format_type(t)
          for t in ("Movie", "Series", "Episode", None)))

print("== 4. 后半段业务链路零改动（静态契约） ==")
for rel, must in (
    (BASE + "notification_bot_library_new_item_service.py",
     ("def handle_library_new_item(bot, item):", "notify_users_by_flag",
      "def _bark_notify_new_item(item):", "log_media_notification")),
    (BASE + "notification_bot_library_new_episode_service.py",
     ("def handle_library_new_episode(bot, data):", "notify_users_by_flag",
      "def _bark_notify_new_episode(data):", "_format_episode_suffix")),
):
    src = read(rel)
    check("业务链路完整：" + rel.split("/")[-1], all(s in src for s in must))
    check("仍引用公共模块且未引入实体化工具：" + rel.split("/")[-1],
          "from app.bot.notification_bot import _library_common" in src
          and "to_plain_text(" not in src and "sanitize_html(" not in src)

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("B1 LIBRARY COMMON EQUIVALENCE CHECKS PASSED")
