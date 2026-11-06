#!/usr/bin/env python3
"""断言 5 项加固改动确实落在源码里，并对关键函数做真实行为测试。

只依赖标准库，不需要安装应用依赖，因此在 CI 里跑得快且稳定。
用法: python tools/verify_patches.py [ROOT]
"""
from __future__ import annotations

import ast
import os
import re
import sys

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
FAILS: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  " + name)
    else:
        FAILS.append(name + ("  " + detail if detail else ""))
        print("  FAIL  " + name + ("  " + detail if detail else ""))


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# 1.9.5：机器人助手页模板（Webhook 整合 + Bark 卡片）在多个断言组中使用，提前读取
bot_tpl = read("templates/bot.html")


def nodes(src: str, names: set[str]):
    """挑出指定名称的顶层赋值/函数/导入节点，供独立 exec 使用。"""
    tree = ast.parse(src)
    out = []
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            # 只保留标准库 os
            if isinstance(n, ast.Import) and any(a.name == "os" for a in n.names):
                out.append(n)
        elif isinstance(n, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id in names for t in n.targets):
                out.append(n)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names:
            out.append(n)
    return out


def exec_nodes(src: str, names: set[str], extra: dict | None = None) -> dict:
    from urllib.parse import urlparse
    ns: dict = {"Request": object, "os": os, "urlparse": urlparse}
    if extra:
        ns.update(extra)
    mod = ast.Module(body=nodes(src, names), type_ignores=[])
    exec(compile(mod, "<hardening-check>", "exec"), ns)
    return ns


class FakeRequest:
    def __init__(self, base_url: str, headers: dict | None = None,
                 query: dict | None = None):
        self.base_url = base_url
        self.headers = headers or {}
        self.query_params = query or {}


print("== 问题 1/2: Cloudflare Tunnel 下的 CSRF Origin ==")
csrf = read("app/core/csrf_middleware.py")
ns = exec_nodes(csrf, {"_expected_origins", "_origin_allowed", "_same_origin"})
check("csrf 模块可提取 _expected_origins", "_expected_origins" in ns)

# 场景 A：uvicorn 已采信代理头（FORWARDED_ALLOW_IPS 生效）
r = FakeRequest("https://pulse.example.com/", {
    "host": "pulse.example.com", "x-forwarded-proto": "https"})
origins = ns["_expected_origins"](r)
check("真实环境允许 https 同源", "https://pulse.example.com" in origins, str(origins))
check("真实环境不残留 http 同源", "http://pulse.example.com" not in origins, str(origins))
check("不放大到任意域名", all("evil." not in o for o in origins), str(origins))

# 场景 B：uvicorn 未采信代理头时，应用层兜底仍要补出 https
r2 = FakeRequest("http://pulse.example.com/", {
    "host": "pulse.example.com", "x-forwarded-proto": "https"})
origins2 = ns["_expected_origins"](r2)
check("应用层兜底补出 https 变体", "https://pulse.example.com" in origins2, str(origins2))

dockerfile = read("Dockerfile")
compose = read("docker-compose.yml")
check("Dockerfile 默认 FORWARDED_ALLOW_IPS", "FORWARDED_ALLOW_IPS" in dockerfile)
check("compose 默认 FORWARDED_ALLOW_IPS", "FORWARDED_ALLOW_IPS" in compose)

print("== 问题 3: 明文传送 token（1.9.0 起兼容 Emby ?token= URL）==")
webhook = read("app/domains/system/webhook.py")
wns = exec_nodes(webhook, {"_get_webhook_token"})
os.environ.pop("WEBHOOK_ALLOW_QUERY_TOKEN", None)
q = FakeRequest("", {}, {"token": "QUERY_SECRET_VALUE"})
check("默认兼容 Emby Webhook 的 ?token= URL",
      wns["_get_webhook_token"](q) == "QUERY_SECRET_VALUE")
check("无 token 时返回 None", wns["_get_webhook_token"](FakeRequest("", {}, {})) is None)
os.environ["WEBHOOK_ALLOW_QUERY_TOKEN"] = "0"
check("显式关闭后回到严格 Header-only",
      wns["_get_webhook_token"](q) is None)
os.environ["WEBHOOK_ALLOW_QUERY_TOKEN"] = "1"
check("显式开启兼容开关仍生效",
      wns["_get_webhook_token"](q) == "QUERY_SECRET_VALUE")
os.environ.pop("WEBHOOK_ALLOW_QUERY_TOKEN", None)
h = FakeRequest("", {"X-Webhook-Token": "HEADER_SECRET"}, {"token": "QUERY_SECRET_VALUE"})
check("请求头优先", wns["_get_webhook_token"](h) == "HEADER_SECRET")
hs = FakeRequest("", {"X-Webhook-Token": "HEADER_SECRET"}, {})
os.environ["WEBHOOK_ALLOW_QUERY_TOKEN"] = "0"
check("严格模式下仍接受请求头", wns["_get_webhook_token"](hs) == "HEADER_SECRET")
os.environ.pop("WEBHOOK_ALLOW_QUERY_TOKEN", None)
check("Dockerfile 默认允许 query token", "WEBHOOK_ALLOW_QUERY_TOKEN=1" in dockerfile)
check("compose 默认允许 query token", "WEBHOOK_ALLOW_QUERY_TOKEN=1" in compose)
check("401 提示同时覆盖 Header 与 URL 两种方式",
      "X-Webhook-Token 请求头" in webhook and "?token=<Token>" in webhook)

print("== 问题 4: 默认拒绝鉴权网关 ==")
gate_path = "app/core/auth_gate_middleware.py"
check("新增 auth_gate_middleware.py", os.path.isfile(os.path.join(ROOT, gate_path)))
gate = read(gate_path)
gns = exec_nodes(gate, {"DEFAULT_PUBLIC_API_PATHS", "ADMIN_ONLY_PREFIXES",
                        "PUBLIC_READ_PREFIXES", "SAFE_METHODS",
                        "_matches", "is_public_api", "is_admin_api"})
os.environ.pop("PUBLIC_API_PATHS", None)
for p in ("/api/history/stats", "/api/pwa/config", "/api/auth/settings",
          "/api/pwa/user_icon", "/api/wallpaper"):
    check("敏感接口不在匿名白名单 " + p, not gns["is_public_api"](p))
check("登录入口仍可匿名", gns["is_public_api"]("/api/login"))
check("Webhook 回调仍可匿名", gns["is_public_api"]("/api/v1/webhook"))
check("管理设置需管理员", gns["is_admin_api"]("/api/bot/settings"))
check("用户管理需管理员", gns["is_admin_api"]("/api/manage/user/new"))
mw = read("app/bootstrap/middleware.py")
check("中间件已注册 AuthGate", "app.add_middleware(AuthGateMiddleware)" in mw)
check("AuthGate 位于会话中间件内层",
      mw.index("app.add_middleware(AuthGateMiddleware)")
      < mw.index("app.add_middleware(DatabaseSessionMiddleware)"))
check("compose 默认 AUTH_GATE_MODE", "AUTH_GATE_MODE" in compose)

print("== 问题 5: F12 泄露后台配置（URL 兼容但不回传明文）==")
bot = read("app/bot/bot.py")
tpl = read("templates/settings.html")
check("后端不把明文 token 拼进 webhook_url", "/api/v1/webhook?token=" not in bot)
check("后端不直接回传明文 webhook_token",
      'all_config["webhook_token"] = webhook_token' not in bot)
check("后端 Webhook Token 统一脱敏",
      'all_config["webhook_token"] = mask_sensitive(webhook_token)' in bot
      and 'all_config["webhook_token"] = "****（由环境变量设置）"' in bot)
check("模板不硬编码完整带 token 的 URL", "webhook?token=" not in tpl)
# 1.9.5：?token= 地址拼装从设置页迁移至机器人助手页（bot.html）
check("机器人助手页拼出 Emby 可用的 ?token= 地址",
      "'?token='" in bot_tpl and "/api/v1/webhook" in bot_tpl)
check("脱敏值不会被误拼进 URL（占位符兜底）",
      "你的Token" in bot_tpl and "raw.indexOf('****')" in bot_tpl)
check("URL 预览基于当前 host", "${host}/api/v1/webhook`" in bot_tpl)
check("复制按钮含 execCommand 降级（内网 http 非安全上下文）",
      "document.execCommand('copy')" in bot_tpl)

print("== 交付与配置隔离 ==")
check("compose 引用自己的镜像", "ghcr.io/abson2/emby-pulse-hardened" in compose)
check("compose 未照搬上游主机路径", "/volume1/docker" not in compose)
check("compose 不含示例内网 IP", "192.168." + "31.2" not in compose)
check("Dockerfile 使用显式 COPY 白名单", "COPY . ." not in dockerfile)
check("存在凭证扫描器", os.path.isfile(os.path.join(ROOT, "tools/check-no-secrets.py")))
check("存在 CI workflow", os.path.isfile(os.path.join(ROOT, ".github/workflows/docker-publish.yml")))
env = read(".env.example")
import re as _re
_literal_secret = _re.compile(
    r"(?m)^[ \t]*[A-Z0-9_]*(?:TOKEN|API_KEY|SECRET|PASSWORD)[A-Z0-9_]*"
    r"[ \t]*=[ \t]*([A-Za-z0-9_+/=-]{16,})"
)
check(".env.example 不含字面量凭证", not _literal_secret.search(env))
_host_ip = _re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b(?!\s*/)"
)
check(".env.example 不含具体内网主机地址", not _host_ip.search(env))

print("== 问题 6: 10307/10308 会话 Cookie 隔离（避免互踢） ==")
sess = read("app/core/session.py")
sns = exec_nodes(sess, {"SESSION_COOKIE_NAME", "USER_PORTAL_SESSION_COOKIE_NAME",
                        "USER_PORTAL_SCOPE_FLAG", "session_cookie_name"})
check("定义管理端 Cookie 名", sns.get("SESSION_COOKIE_NAME") == "session_id")
check("定义用户端独立 Cookie 名",
      bool(sns.get("USER_PORTAL_SESSION_COOKIE_NAME"))
      and sns["USER_PORTAL_SESSION_COOKIE_NAME"] != sns["SESSION_COOKIE_NAME"])
check("管理端 scope 用默认名", sns["session_cookie_name"]({"type": "http"}) == "session_id")
check("用户端 scope 用独立名",
      sns["session_cookie_name"]({sns["USER_PORTAL_SCOPE_FLAG"]: True})
      == sns["USER_PORTAL_SESSION_COOKIE_NAME"])
check("伪造标记不会被误判（仅认布尔真值）",
      sns["session_cookie_name"]({sns["USER_PORTAL_SCOPE_FLAG"]: "false"}) == "session_id")
check("非 dict scope 安全降级", sns["session_cookie_name"](None) == "session_id")

smw = read("app/core/session_middleware.py")
check("中间件按请求动态选 Cookie 名", "session_cookie_name(request.scope)" in smw)
check("读取 Cookie 用动态名", "request.cookies.get(cookie_name)" in smw)
check("写回 Cookie 用动态名", "key=cookie_name" in smw)
check("中间件不再硬编码 session_id 读写", "cookies.get(SESSION_COOKIE_NAME)" not in smw)

portal = read("app/bootstrap/user_portal.py")
check("用户端 wrapper 打 scope 标记", "USER_PORTAL_SCOPE_FLAG" in portal
      and "scope[USER_PORTAL_SCOPE_FLAG] = True" in portal)
check("标记在进入主 app 之前写入",
      portal.index("scope[USER_PORTAL_SCOPE_FLAG] = True")
      < portal.index("await app(scope, receive, send)"))

print("== 问题 7: TgtoDrive 规则字段契约（依据真实响应样本定稿） ==")
tg = read("app/plugins/tg_whitelist_sync/plugin.py")
check("必含字段真名 required_keyword", '"required": "required_keyword"' in tg)
check("必含开关 include_required_keyword", '"include_required_keyword"' in tg)
check("画面效果真名 effect", '"effect": "effect"' in tg)
check("RULE_FIELD 映射未回退到旧推断名",
      '"required": "must_include"' not in tg
      and '"effect": "picture_effect"' not in tg)
check("按目标场景定位 rules", "_find_scene_container" in tg)
check("meta_raw_regex 纳入清理", '"meta_raw_regex"' in tg)
check("旧配置错误字段名自动纠正", '"must_include", "picture_effect"' in tg)
check("写入体形态可切换", '"write_mode"' in tg and '"envelope"' in tg)

print("== 问题 8: 工单删除联动移除 TgtoDrive 监控关键词 ==")
mr = read("app/domains/media_requests/management_router.py")
check("插件 A 提供删单联动入口", "def remove_items(self, candidates):" in tg)
check("client 提供安全移除能力", "def remove_rules(self, rule_ids):" in tg)
check("插件 A 版本升级到 2.2.0", 'version = "2.2.0"' in tg)
check("只接受 kw_ 前缀 id（绝不误删用户规则）",
      "rid.startswith(RULE_ID_PREFIX)" in tg)
check("保留非目标规则", "kept.append(r)" in tg)
check("删除体只写 kept 规则", 'payload["rules"] = list(kept)' in tg)
check("空配置护栏（绝不 POST 空规则）",
      "if not isinstance(cfg, dict) or not cfg:" in tg)
check("定位不到目标场景时中止删除", "为安全起见中止删除" in tg)
check("未命中目标规则时不写入", "if not removed:" in tg)
check("删单联动支持 dry_run", "删单联动将移除" in tg)
check("删除入口收集 (tmdb_id, season)",
      "remove_candidates.append((tid, sn))" in mr)
check("仅 delete 动作触发联动",
      'data.action == "delete" and remove_candidates' in mr)
check("异步触发函数已定义", "def _trigger_tg_whitelist_remove(candidates):" in mr)
check("守护线程调用 remove_items", "plugin.remove_items(candidates)" in mr)
check("原推送事件驱动未被破坏",
      "plugin.push_items(candidates, set_status_on_success=1)" in mr
      and 'if data.action in ("approve", "manual") and push_candidates:' in mr)
check("模块级 logger 已定义（修复 push 钩子异常分支）",
      'logger = logging.getLogger("uvicorn")' in mr)

print("== 问题 9: 系统级 Bark 推送 + Emby Webhook 兼容（1.9.0） ==")
bark = read("app/domains/notifications/bark_push.py")
ncfg = read("app/infra/config/notification_settings.py")
submit = read("app/domains/media_requests/submit_router.py")
nitem = read("app/bot/notification_bot/notification_bot_library_new_item_service.py")
nep = read("app/bot/notification_bot/notification_bot_library_new_episode_service.py")
nadmin = read("app/domains/notifications/notify_admin.py")
ntpl = read("templates/notify_admin.html")
settings_tpl = read("templates/settings.html")
system_router = read("app/domains/system/router.py")

check("系统级 Bark 模块存在且提供统一入口",
      "def safe_push(kind, **kwargs):" in bark)
check("提供新片入库推送", "def push_new_library_item(" in bark)
check("提供新的求片推送", "def push_new_request(" in bark)
check("提供测试推送", "def send_test(" in bark)
check("两类消息各有独立开关",
      '"notify_library_new"' in bark and '"notify_request_new"' in bark)
check("敏感 Key 从返回结构中剔除", 'if k != "device_key"' in bark)
check("失败吞掉异常且只记日志",
      "except Exception as e:" in bark and "logger.warning" in bark)
check("只使用标准库（urllib，不引第三方 SDK）",
      "import urllib.request" in bark and "import requests" not in bark)

check("Bark 配置层提供运行期读取与保存",
      "def get_bark_runtime_config() -> dict:" in ncfg
      and "def set_bark_config(data: dict) -> None:" in ncfg)
check("管理端读取 Bark 配置做掩码",
      '"bark": get_bark_config(masked=masked)' in ncfg)
check("保存时掩码值不覆盖真实 Device Key",
      '"****" not in key' in ncfg)
check("系统设置保存接口可写入 Bark 配置",
      'if "bark" in data:' in ncfg and "set_bark_config(data[\"bark\"])" in ncfg)

check("求片提交事件接入 Bark",
      '"request_new"' in submit and "_bark_push_provider" in submit)
check("新片入库事件接入 Bark（电影/媒体项目）",
      '"library_new"' in nitem and nitem.count("_bark_notify_new_item(item)") >= 2)
check("剧集新集入库事件接入 Bark",
      '"library_new"' in nep and nep.count("_bark_notify_new_episode(data)") >= 2)
check("Bark 采用可注入 provider（不破坏边界测试）",
      "bark_push_provider=None" in submit
      and "bark_push_provider=None" in nitem
      and "bark_push_provider=None" in nep)
check("管理端提供 Bark 测试推送接口",
      '@router.post("/api/notify/bark_test")' in nadmin)
check("测试推送接口要求管理员", "user_service.is_admin_user(request)" in nadmin)
check("通知管理页提供 Bark 配置卡片",
      "bark-enabled" in ntpl and "testBark()" in ntpl)
check("通知管理页两类消息开关独立",
      "bark-notify-library-new" in ntpl and "bark-notify-request-new" in ntpl)

check("设置页不再错误地只推荐 Header 方式",
      "推荐使用 Header 方式传递 Token，更安全" not in settings_tpl)
check("机器人助手页明确说明 Emby 无法附带请求头",
      "Emby 插件无法附带请求头" in bot_tpl)
check("Webhook 复制入口已整合至机器人助手页（1.9.5）",
      "copyBotWebhookUrl()" in bot_tpl and "buildBotWebhookUrl()" in bot_tpl)
check("机器人助手页 Webhook 地址带 ?token=（脱敏值不拼入）",
      "'?token=' + botWebhookTokenForUrl()" in bot_tpl and "你的Token" in bot_tpl)
check("设置页 Webhook 区已整体删除（1.9.6，不再保留引导卡）",
      "前往 通知与机器人 配置" not in settings_tpl
      and 'id="webhook_token"' not in settings_tpl
      and "系统神经感知" not in settings_tpl)
check("系统设置接口给出可复制的 URL 模板",
      "?token=<WEBHOOK_TOKEN>" in system_router
      and "webhook_query_token_enabled" in system_router)
check("系统设置接口不再要求 Emby 配置 Header",
      "请在 Emby 中配置 Header" not in system_router)

check("应用版本升级到 1.9.7", 'APP_VERSION = "1.9.7"' in read("app/shared/version.py"))
check("Dockerfile 版本为 1.9.7-hardened", "APP_VERSION=1.9.7-hardened" in dockerfile)
check("compose 默认拉取 1.9.7", "IMAGE_TAG:-1.9.7" in compose)
check("CHANGELOG 含 1.9.0 / 1.9.1 / 1.9.2 / 1.9.6 / 1.9.7 发版说明",
      all(x in read("CHANGELOG.md") for x in ("## 1.9.0", "## 1.9.1", "## 1.9.2", "## 1.9.6", "## 1.9.7")))
check("CI 增加 Bark / Webhook 回归步骤",
      "tests/test_bark_push.py" in read(".github/workflows/docker-publish.yml"))

print("== 问题 10: Bark/Webhook 模块已从系统设置页删除（1.9.6，入口收敛至机器人助手） ==")
settings = tpl
layout = read("templates/layout.html")
changelog = read("CHANGELOG.md")
check("系统设置页不再有「系统级 Bark 推送」卡片",
      "系统级 Bark 推送" not in settings and "settings-bark-enabled" not in settings)
check("Bark 配置字段与按钮已全部移除",
      all(x not in settings for x in (
          "settings-bark-server", "settings-bark-device-key",
          "settings-bark-sound", "settings-bark-group",
          "settings-bark-notify-library-new", "settings-bark-notify-request-new",
          "saveBarkSettings()", "testBarkSettings()")))
check("Bark 读写 JS 函数已移除",
      all(x not in settings for x in (
          "function collectBarkSettings()", "async function loadBarkConfig()",
          "async function saveBarkSettings()", "async function testBarkSettings()",
          "loadBarkConfig();")))
check("系统设置页不再有「系统神经感知 (Webhook)」卡片",
      "系统神经感知" not in settings)
check("Bark 与 Webhook 配置收敛至机器人助手页（bot.html）",
      'id="bot-bark-enabled"' in bot_tpl
      and 'id="webhook_token_bot"' in bot_tpl
      and "'/api/notify/channels_config'" in bot_tpl)
check("侧边栏未新增独立通知管理板块",
      'href="/notify_admin" class="nav-item' not in layout)
check("CHANGELOG 含 1.9.6 删除说明",
      "## 1.9.6" in changelog and "删除" in changelog)

print("== 问题 11: 入库事件订阅不再依赖 TG/企微（1.9.2） ==")
bot_svc = read("app/bot/notification_bot/bot_service.py")
ci = read(".github/workflows/docker-publish.yml")
check("start 去掉渠道凭证提前 return",
      "if not get_bot_tg_token() and not get_wecom_corpid(): return" not in bot_svc)
check("start 无条件建立事件订阅与 running 标记",
      "self._subscribe_events()" in bot_svc and "self.running = True" in bot_svc)
check("外部动作按 has_tg / has_wecom 精确门控",
      "has_tg = bool(get_bot_tg_token())" in bot_svc
      and "has_wecom = bool(get_wecom_corpid())" in bot_svc
      and "if has_tg:" in bot_svc and "if has_wecom:" in bot_svc)
check("轮询线程仍只在有 TG Token 时启动",
      'name="notification-bot-polling"' in bot_svc)
check("_subscribe_events 仍订阅两个入库事件",
      'bus.subscribe("notify.library.new_item", self.on_library_new_item)' in bot_svc
      and 'bus.subscribe("notify.library.new_episode", self.on_library_new_episode)' in bot_svc)
_item_body = nitem.split("def handle_library_new_item(", 1)[1]
_ep_body = nep.split("def handle_library_new_episode(", 1)[1]
check("Bark handler 位于入库通知总开关判断之前",
      _item_body.index("_bark_notify_new_item(item)") < _item_body.index("if not _enable_library_notify_provider()():")
      and _ep_body.index("_bark_notify_new_episode(data)") < _ep_body.index("if not _enable_library_notify_provider()():"))
check("CI 增加入库事件订阅行为校验",
      "tools/check_notification_bot_subscription.py" in ci)
check("新增行为校验脚本存在且只依赖标准库",
      "import ast" in read("tools/check_notification_bot_subscription.py")
      and "import requests" not in read("tools/check_notification_bot_subscription.py"))
check("CHANGELOG 含 1.9.2 修复说明",
      "## 1.9.2" in changelog and "静默" in changelog)

print("== 问题 12: v1.9.5~1.9.7 反馈（通知设置 / Bark 入口 / Webhook 整合 / 批量栏横滑） ==")
req_tpl = read("templates/request.html")
req_js = read("static/js/request_app.js")
tab_profile = read("templates/request/tab_profile.html")
req_admin = read("templates/requests_admin.html")
bot_py = read("app/bot/bot.py")
models_py = read("app/schemas/models.py")
sys_views = read("app/domains/system/views.py")
modal_notify = read("templates/request/modal_notify.html")

check("request_app.js 引用带版本号（浏览器缓存破坏，1.9.7）",
      "request_app.js?v=1.9.7" in req_tpl)
# 1.9.6 起通知设置不再由 Alpine 主组件承载（1.9.7 改为页面内原生弹窗）
check("request_app.js 不再包含任何通知设置 state/方法（1.9.6 起移除）",
      all(x not in req_js for x in (
          "notifySettingsModalOpen", "loadNotifySettings",
          "saveNotifySettings", "setNotifyChannel", "notifySettings")))
# 🔥 1.9.7：按用户要求回归页面内弹窗，不再打开独立页面
check("tab_profile 通知设置按钮改为页面内弹窗",
      'onclick="epNotifyOpen()"' in tab_profile
      and 'href="/user/notify_settings"' not in tab_profile)
check("三按钮一排且通知设置位于媒体库设置与娱乐功能中间",
      "grid grid-cols-3" in tab_profile
      and tab_profile.index("libSettingsModalOpen = true")
          < tab_profile.index('onclick="epNotifyOpen()"')
          < tab_profile.index("entertainmentModalOpen = true"))
check("tab_profile 不再有通知设置 teleport 弹窗",
      "notifySettingsModalOpen" not in tab_profile
      and "setNotifyChannel" not in tab_profile)
check("页面内弹窗随 HTML 下发（纯原生 JS，无 Alpine / teleport 属性）",
      "epNotifyOpen" in modal_notify
      and "epNotifySave" in modal_notify
      and "/api/user/notify_settings" in modal_notify
      and 'x-data="' not in modal_notify
      and 'x-teleport="' not in modal_notify)
check("request.html include 通知设置弹窗",
      "{% include 'request/modal_notify.html' %}" in req_tpl)
check("废弃的独立通知设置页已删除（不再单独打开页面）",
      not os.path.exists(os.path.join(ROOT, "templates/user_notify.html")))
check("旧独立页链接 303 回「我的」并自动打开弹窗",
      'RedirectResponse("/request?tab=profile&notify=1", status_code=303)' in sys_views
      and "notify=1" in modal_notify)
check("10308 白名单放行旧链接避免 403",
      '"/user/notify_settings"' in read("app/bootstrap/user_portal.py"))
check("BotSettingsModel 含 webhook_token 字段（修复保存 500）",
      "webhook_token: Optional[str]" in models_py)
check("系统设置页 Webhook/Bark 模块已删除（1.9.6）",
      "系统神经感知" not in tpl and "系统级 Bark 推送" not in tpl)
check(".env 控制 token 时输入框不禁用（粘贴原值仅生成地址，保存不覆盖）",
      "envPasteMode" in bot_tpl
      and "可粘贴 .env 原值" in bot_tpl)
check("推送场景总控新增 Bark 推送渠道勾选",
      'id="notify_channel_bark"' in bot_tpl)
check("推送场景总控新增 Bark 推送配置卡片",
      'id="bot-bark-enabled"' in bot_tpl
      and 'id="bot-bark-server"' in bot_tpl
      and 'id="bot-bark-device-key"' in bot_tpl
      and 'id="bot-bark-notify-library-new"' in bot_tpl
      and 'id="bot-bark-notify-request-new"' in bot_tpl)
check("Bark 卡片保存/测试走 /api/notify 专用接口",
      "collectBotBarkSettings()" in bot_tpl
      and "loadBotBarkConfig()" in bot_tpl
      and "'/api/notify/channels_config'" in bot_tpl
      and "'/api/notify/bark_test'" in bot_tpl)
check("入库通知渠道保存包含 bark",
      "?(['bark'] : [])" in bot_tpl or "['bark']" in bot_tpl)
check("机器人助手页 Webhook Token 输入与保存",
      'id="webhook_token_bot"' in bot_tpl
      and "webhook_token: getSensitiveValue('webhook_token_bot')" in bot_tpl)
check("bot settings 保存接口支持 webhook_token（脱敏/env 不覆盖）",
      'should_update_sensitive_bot_setting("webhook_token", data.webhook_token)' in bot_py
      and 'set_bot_setting("webhook_token"' in bot_py)
check("工单大厅批量悬浮栏支持拖拽横滑 + iOS 惯性滚动",
      "startBatchDrag" in req_admin and "endBatchDrag" in req_admin
      and "-webkit-overflow-scrolling: touch" in req_admin)
check("CHANGELOG 含 1.9.6 发版说明", "## 1.9.6" in changelog)

print("== 问题 13: v1.9.7 入库 Bark 修复 + 管理端 Bark 真开关 ==")
bark_py = read("app/domains/notifications/bark_push.py")
ncfg_py = read("app/infra/config/notification_settings.py")
nadmin_py = read("app/domains/notifications/notify_admin.py")
users_tpl = read("templates/users.html")

# 配置边界：WEBHOOK_TOKEN 属于 Webhook 回调校验链路，绝不能被当成 Bark 配置
check("Bark 配置不引入 BARK_* 环境变量（不与 WEBHOOK_TOKEN 混淆）",
      "BARK_ENV_FIELDS" not in ncfg_py
      and "os.getenv" not in ncfg_py
      and "WEBHOOK_TOKEN" in ncfg_py)
check("Bark 配置仍只从 config.json 读写",
      'cfg.get("bark_device_key"' in ncfg_py
      and "def set_bark_config(data: dict) -> None:" in ncfg_py)
check("is_bark_channel_selected 按通知类型精确判断",
      "def is_bark_channel_selected(notify_type" in ncfg_py
      and "get_notify_rule_row" in ncfg_py)
check("safe_push 借鉴企微 channels 驱动，按事件类型应用门控",
      "_RULE_TYPE_BY_KIND" in bark_py
      and "def _apply_channel_gate(" in bark_py)
check("channels 门控仅作用于未注入 config 的真实运行路径",
      'if not isinstance(kwargs.get("config"), dict):' in bark_py)
check("未推送原因写入日志便于用户自证",
      "未推送原因" in bark_py)
check("顶部「保存配置」一并提交 Bark 卡片（修复 Key 未落盘/被重载清空）",
      "await saveBotBarkSettings({silent: true})" in bot_tpl
      and "return ok;" in bot_tpl)
check("管理端通知设置渠道定义含 Bark",
      '{"id": "bark"' in nadmin_py)
check("工单大厅四类通知各含 Bark 勾选",
      all('data-type="%s" data-channel="bark"' % t in req_admin
          for t in ("request_new", "request_update",
                    "request_status", "feedback_new")))
check("用户管理三类通知各含 Bark 勾选",
      all('data-type="%s" data-channel="bark"' % t in users_tpl
          for t in ("user_register", "user_login", "user_delete")))
check("管理端 6 个通知触发点全部接入 safe_rule_push（真开关）",
      sum(1 for p in (
          "app/domains/media_requests/feedback_router.py",
          "app/domains/media_requests/registration_router.py",
          "app/domains/users/delete_router.py",
          "app/bot/notification_bot/notification_bot_user_login_service.py",
          "app/domains/media_requests/update_router.py",
          "app/domains/media_requests/management_router.py",
      ) if "safe_rule_push(" in read(p)) == 6)
check("push_rule_notification / safe_rule_push 已实现",
      "def push_rule_notification(" in bark_py
      and "def safe_rule_push(" in bark_py)
check("CHANGELOG 含 1.9.7 发版说明", "## 1.9.7" in changelog)

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL HARDENING CHECKS PASSED")
