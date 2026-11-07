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

check("应用版本升级到 2.5", 'APP_VERSION = "2.5"' in read("app/shared/version.py"))
check("Dockerfile 版本为 2.5-hardened", "APP_VERSION=2.5-hardened" in dockerfile)
check("compose 默认拉取 2.5", "IMAGE_TAG:-2.5" in compose)
check("compose 保留 1.9.10 回退说明（不删除历史版本特性）", "IMAGE_TAG=1.9.10" in compose)
check("CHANGELOG 含 1.9.x 全系列与 2.0 / 2.0.1 / 2.0.2 / 2.1 / 2.3 / 2.4 发版说明",
      all(x in read("CHANGELOG.md") for x in ("## 1.9.0", "## 1.9.1", "## 1.9.2", "## 1.9.6", "## 1.9.7", "## 1.9.8", "## 1.9.9", "## 1.9.10", "## 2.0", "## 2.0.1", "## 2.0.2", "## 2.1", "## 2.3", "## 2.4", "## 2.5")))
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

check("request_app.js 引用带版本号（浏览器缓存破坏，2.5）",
      "request_app.js?v=2.5" in req_tpl)
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
check("Bark Device Key 经 config.py 中央注册 BARK_DEVICE_KEY（与 WEBHOOK_TOKEN 同级安全处理）",
      'BARK_DEVICE_KEY' in read("app/core/config.py")
      and '"bark_device_key"' in read("app/core/config.py"))
check("Bark 配置层不直接读 os.getenv（统一走中央注册，避免分散）",
      "os.getenv" not in ncfg_py)
check("get_bark_config 回显 env 来源与只读标记",
      "device_key_source" in ncfg_py and "device_key_readonly" in ncfg_py)
check(".env.example 文档化 BARK_DEVICE_KEY（管理端系统级 Bark）",
      "BARK_DEVICE_KEY" in env)
check("Bark 配置仍只从 config.json 读写（env 覆盖时只读）",
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

print("== 问题 14: v1.9.8 Emby Webhook 事件名映射与 LibraryChanged 拆条 ==")
we_py = read("app/domains/system/webhook_events.py")
wh_py = read("app/domains/system/webhook.py")
we_test = read("tests/test_webhook_events.py")

check("事件映射模块存在且含 Emby→内部映射表",
      "EMBY_TO_INTERNAL" in we_py
      and "itemadded" in we_py and "librarychanged" in we_py)
check("映射覆盖入库 / 播放 / 登录 / 删除",
      all(k in we_py for k in ("itemadded", "librarychanged", "playbackstart",
                               "playbackstop", "authenticationsuccess", "itemremoved")))
check("已是内部格式的事件名保持原样不被打散",
      "KNOWN_INTERNAL_EVENTS" in we_py and '"library.new"' in we_py)
check("LibraryChanged 按 ItemsAdded 数组拆成 item.added",
      "def split_library_changed(" in we_py
      and "ItemsAdded" in we_py and '"item.added"' in we_py)
check("拆条保留对象元素的 Type/Name 等字段",
      "dict(raw)" in we_py)
check("Item Added + Library Changed 双事件短期去重",
      "def mark_item_seen(" in we_py
      and "def reset_dedupe_cache(" in we_py)
check("未识别事件按名称限流告警（不刷屏）",
      "def warn_unknown_event_once(" in we_py and "_unseen_events" in we_py)
check("webhook.py 接入归一化 / 拆条 / 去重",
      all(x in wh_py for x in (
          "webhook_events.get_raw_event(data)",
          "webhook_events.normalize_event(",
          "webhook_events.split_library_changed(",
          "webhook_events.mark_item_seen(")))
check("webhook.py 拆条后逐条发布 item.added",
      'bus.publish("webhook.received", "item.added", payload)' in wh_py)
check("webhook.py 接入未识别事件告警",
      "warn_unknown_event_once" in wh_py)
check("行为回归覆盖端到端入队与旧缺陷复现",
      "handle_webhook_event(" in we_test
      and "EMBY WEBHOOK EVENT CHECKS PASSED" in we_test
      and "itemadded 不入队" in we_test)
check("CI 已接入 Emby 事件映射回归",
      "tests/test_webhook_events.py" in ci)
check("CHANGELOG 含 1.9.8 发版说明", "## 1.9.8" in changelog)

print("== 问题 15: v1.9.9 抑制 Emby 非业务事件告警 / 误判 ==")
we15 = read("app/domains/system/webhook_events.py")
wh15 = read("app/domains/system/webhook.py")

check("事件模块定义已知可忽略事件集合", "IGNORE_EVENT_KEYS" in we15)
check("忽略集合覆盖 LibraryDeleted（整个媒体库被移除，非单项媒体删除）",
      "librarydeleted" in we15)
check("忽略集合覆盖系统测试通知", "systemnotificationtest" in we15)
check("忽略集合覆盖高频会话/保存事件",
      all(k in we15 for k in ("sessionstarted", "sessionstopped", "userdatasaved")))
check("提供 is_ignorable_event 判定函数", "def is_ignorable_event(" in we15)
check("webhook.py 接入忽略门控（早返回，不告警、不发布）",
      "is_ignorable_event(raw_event)" in wh15
      and 'return {"status": "success", "message": "ignored"}' in wh15)
check("忽略判定位于重要事件日志与告警之前（避免刷屏）",
      wh15.index("is_ignorable_event(raw_event)")
      < wh15.index("important_events = ["))
check("library.deleted 不会进入下游事件总线（否则被 'delete' in event 误判为媒体删除通知）",
      # 复刻 webhook.py 门控：可忽略事件在 publish 前已 return
      "if webhook_events.is_ignorable_event(raw_event):" in wh15)
check("CI 已接入 Emby 事件映射回归（含 1.9.9 忽略断言）",
      "tests/test_webhook_events.py" in ci)
check("CHANGELOG 含 1.9.9 发版说明", "## 1.9.9" in changelog)

print("== 问题 16: v2.0 架构优化 / 安全加固 / 公告推送乱码修复 ==")
pdao = read("app/plugins/plugin_dao.py")
reg = read("app/infra/db/schema_registry.py")
mdao = read("app/domains/notifications/message_dao.py")
secu = read("app/core/security_utils.py")
ann_router = read("app/domains/notifications/announcements_router.py")
explore_tpl = read("templates/request/tab_explore.html")
refactor_doc = "docs/REFACTOR_2.0.md"

check("存在 2.0 架构/重构/安全方案文档", os.path.isfile(os.path.join(ROOT, refactor_doc)))
check("插件日志改为单连接单事务（3 次连接 → 1 次）",
      "with system_store.connect() as conn:" in pdao
      and "def add_plugin_log(" in pdao)
check("行数裁剪改为主键范围删除（等价高效）",
      "SELECT MIN(id) FROM (" in pdao
      and "AND id NOT IN (" not in pdao
      and "ORDER BY id DESC LIMIT ?" in pdao)
check("plugin_logs 增补 created_at 索引（纯加法）",
      "idx_plugin_logs_created_at" in reg)
check("公告表 bootstrap 加进程内缓存与 reset 钩子",
      "_announcement_tables_ready" in mdao
      and "def reset_announcement_tables_cache()" in mdao)
check("公告解码循环抽公共 helper",
      "def _decode_rows_content(" in ann_router
      and len(re.findall(r"^\s+_decode_rows_content\(announcements\)", ann_router, re.M)) == 2
      and ann_router.count("for ann in announcements:") == 1)
check("新增 to_plain_text 纯文本通道工具",
      "def to_plain_text(" in secu and "_DANGEROUS_PATTERNS" in secu)
check("用户级公告通知正文/标题改用 to_plain_text（修复 &lt;p&gt; 乱码）",
      "to_plain_text(data.content, max_length=2000)" in ann_router
      and "to_plain_text(data.title, max_length=500)" in ann_router)
check("网页端富文本存储链路未被改动",
      "sanitize_rich_html(data.content, max_length=50000)" in ann_router
      and "def sanitize_rich_html(" in secu)
check("用户端 x-html 统一净化且 CDN 失败可降级",
      "window.sanitizeRichText = function(" in req_js
      and "sanitizeRichText(parseMarkdown(" in explore_tpl
      and "sanitizeRichText(welcomeModal.message)" in req_tpl
      and "dompurify@3" in req_tpl)
check("日志不再输出 Token 片段 / emby_host 明文",
      "new_token[:8]" not in read("app/core/security_check.py")
      and "emby_host: {self.config.get('emby_host', '未设置')}" not in read("app/core/config.py"))
check("CI 接入 2.0 等价性回归",
      "tests/test_refactor_2_0_equivalence.py" in ci)
check("2.0 等价性测试脚本存在且只用标准库",
      os.path.isfile(os.path.join(ROOT, "tests/test_refactor_2_0_equivalence.py"))
      and "import pytest" not in read("tests/test_refactor_2_0_equivalence.py"))
check("对外 API 路由未删减（公告 7 条）",
      all(p in ann_router for p in (
          '"/api/announcements"', '"/api/announcements/{ann_id}"',
          '"/api/announcements/{ann_id}/view"', '"/api/user/announcements"',
          '"/api/user/announcements/{ann_id}/read"')))

print("== 问题 17: v2.0 第二批零风险加固（同类乱码收尾 / 安全契约固化） ==")
msg17 = read("app/domains/notifications/messages.py")
msg17_tree = ast.parse(msg17)
push17 = next(
    x for x in ast.walk(msg17_tree)
    if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
    and x.func.attr == "push_user_notify"
)
push17_src = ast.get_source_segment(msg17, push17) or ""

check("管理员消息用户级通知正文改用 to_plain_text（纯文本通道不实体化）",
      "to_plain_text(data.content, max_length=5000)" in push17_src)
check("该调用内不再使用 sanitize_html 转义", "sanitize_html(" not in push17_src)
check("对外参数 / 标题 / flag key 保持不变",
      all(s in push17_src for s in (
          "data.user_id", '"管理员消息"', 'flag_key="notify_admin_message"')))
check("网页端消息存储仍用 sanitize_html（显示链路零改动）",
      msg17.count("insert_admin_message(") >= 2
      and msg17.count("sanitize_html(data.content, max_length=5000)") >= 3)
check("安全工具导入为追加式（原有导入未删）",
      "from app.core.security_utils import safe_error_message, sanitize_html, to_plain_text" in msg17)

for rel17, expect17 in (
    ("app/bot/notification_bot/notification_bot_library_new_item_service.py", "notify_users_by_flag"),
    ("app/bot/notification_bot/notification_bot_library_new_episode_service.py", "notify_users_by_flag"),
    ("app/domains/media_requests/management_router.py", "notify_request_users"),
):
    s17 = read(rel17)
    check("未改动用户级通知入口：%s" % rel17.split("/")[-1],
          expect17 in s17 and "to_plain_text(" not in s17 and "sanitize_html(" not in s17)

sec17_path = "tools/check_security_contracts.py"
sec17 = read(sec17_path)
check("存在 2.0 安全契约脚本（纯静态、只读源码）",
      os.path.isfile(os.path.join(ROOT, sec17_path))
      and "SECURITY CONTRACT CHECKS PASSED" in sec17)
# 只用 AST 检查真实 import：契约脚本自身含 "import bleach" 等字面量用于检查
# security_utils.py，裸子串判断会自匹配误伤。
sec17_tree = ast.parse(sec17)
sec17_imports = set()
for _n17 in ast.walk(sec17_tree):
    if isinstance(_n17, ast.Import):
        sec17_imports.update(_a.name.split(".")[0] for _a in _n17.names)
    elif isinstance(_n17, ast.ImportFrom) and _n17.module:
        sec17_imports.add(_n17.module.split(".")[0])
check("安全契约脚本只依赖标准库",
      sec17_imports <= {"__future__", "importlib", "os", "sys"},
      repr(sorted(sec17_imports)))
check("安全契约覆盖 10 类既有防护",
      all(s in sec17 for s in (
          "CSRF", "AuthGate", "SESSION_COOKIE_NAME", "script-src-elem",
          "validate_proxy_url", '".." in filename', "SENSITIVE_ENV_FIELDS",
          "sanitize_rich_html", "to_plain_text", "check-no-secrets")))
check("CI 接入安全契约与富文本净化回归",
      "tools/check_security_contracts.py" in ci
      and "tests/test_sanitize_rich_html.py" in ci)
check("等价性回归扩展第二批第 8 节",
      "== 8. 第二批" in read("tests/test_refactor_2_0_equivalence.py"))
check("2.5 发版版本点已同步（历史 tag 不重推）",
      'APP_VERSION = "2.5"' in read("app/shared/version.py")
      and "IMAGE_TAG:-2.5" in read("docker-compose.yml")
      and "request_app.js?v=2.5" in read("templates/request.html"))
check("CHANGELOG 记录第二批零风险收尾", "第二批" in changelog)
check("重构文档记录第二批决策与 B0 收口建议撤销",
      "## 7. 第二批零风险加固" in read("docs/REFACTOR_2.0.md")
      and "不采纳" in read("docs/REFACTOR_2.0.md"))

print("== 问题 18: v2.0.1 主页文档收敛（功能清单 + 用法，细节留各版本） ==")
readme = read("README.md")
check("主页保持既有 4 段式结构",
      all(h in readme for h in (
          "## 一、原项目继承的内容", "## 二、本项目新增功能",
          "## 三、使用说明", "## 四、许可证与开源协议")))
check("主页只列功能与用法，不堆按版本罗列的修复史",
      len(readme.splitlines()) <= 200
      and not re.search(r"^### 2\.\d+\s", readme, re.M)
      and "症状" not in readme)
check("主页指向 CHANGELOG / Releases 查看详细更新说明",
      "CHANGELOG.md" in readme and "GitHub Releases" in readme)
check("主页版本锁定与实际 compose 一致",
      "IMAGE_TAG:-2.5" in readme
      and "ghcr.io/abson2/emby-pulse-hardened:${IMAGE_TAG:-2.5}" in readme)

print("== 问题 19: 2.0 主线优化 B 批（公共逻辑抽离 / 性能 / 安全） ==")
LIB_BASE = "app/bot/notification_bot/"
item19 = read(LIB_BASE + "notification_bot_library_new_item_service.py")
ep19 = read(LIB_BASE + "notification_bot_library_new_episode_service.py")
check("B1 新增入库通知公共模块",
      os.path.isfile(os.path.join(ROOT, LIB_BASE + "_library_common.py"))
      and "class _RichHtmlSanitizer" not in read(LIB_BASE + "_library_common.py"))
check("B1 两个 service 均接入公共模块且对外符号保留",
      all("from app.bot.notification_bot import _library_common" in s
          and "def set_dependency_providers(" in s
          and "def _get_base_url():" in s
          and "def _get_platform(channels):" in s
          for s in (item19, ep19))
      and "def handle_library_new_item(bot, item):" in item19
      and "def handle_library_new_episode(bot, data):" in ep19)
check("B1 装配门控保持各模块状态独立（不共享全局 provider）",
      all("apply_dependency_providers(" in s and "globals()" in s
          for s in (item19, ep19))
      and "_PROVIDER_ATTRS" in read(LIB_BASE + "_library_common.py")
      and "namespace[attr] = value" in read(LIB_BASE + "_library_common.py"))
check("B3 新增通用 EnsureOnce 门控（异常不写缓存 / reset / 按 key 失效）",
      "class EnsureOnce" in read("app/infra/db/ensure_once.py")
      and "def reset(" in read("app/infra/db/ensure_once.py")
      and "def run(" in read("app/infra/db/ensure_once.py"))
check("B3 公告表缓存迁到通用门控且原符号不变",
      all(s in read("app/domains/notifications/message_dao.py") for s in (
          "from app.infra.db.ensure_once import EnsureOnce",
          "_announcement_tables_ready", "_announcement_tables_lock",
          "def reset_announcement_tables_cache()", "def ensure_announcement_tables()")))
check("B4 新增 execute_many（单事务/回滚）且 execute 原语义不变",
      "def execute_many(self, statements) -> int:" in read("app/infra/db/system_store.py")
      and "conn.rollback()" in read("app/infra/db/system_store.py")
      and "def execute(self, sql: str, params=()) -> int:" in read("app/infra/db/system_store.py"))
check("S5 富文本 style 危险值清洗为纯收紧（合法声明不动）",
      "_RICH_UNSAFE_STYLE" in read("app/core/security_utils.py"))
check("B 批等价性回归接入 CI",
      all(s in ci for s in (
          "tests/test_b1_library_common_equivalence.py",
          "tests/test_b_batch_equivalence.py")))
check("B 批不触碰对外接口与用户级通知入口",
      all(s in read("app/domains/users/user_notify_service.py") for s in (
          "def push_user_notify(", "def notify_users_by_flag(",
          "def notify_request_users(")))

print("== 问题 20: 2.0 主线优化 B3-B（ensure_* 纯建表样板抽离） ==")
ddl_helper_rel = "app/infra/db/ddl_bootstrap.py"
ddl_helper = read(ddl_helper_rel)
_helper_fn20 = next(n for n in ast.parse(ddl_helper).body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "ensure_registered_tables")
_helper_body20 = ast.get_source_segment(ddl_helper, _helper_fn20) or ""
check("B3-B 新增无状态纯函数 helper（显式传入 store / ensure_fn）",
      os.path.isfile(os.path.join(ROOT, ddl_helper_rel))
      and "def ensure_registered_tables(store, ensure_fn, table_names)" in _helper_body20
      and "system_store" not in _helper_body20
      and "import " not in _helper_body20
      and "store.connect()" in _helper_body20 and "conn.commit()" in _helper_body20)
# 用 AST 统计「函数体内真实调用」：helper 定义行 def ensure_registered_tables(
# 会让裸子串自匹配成 21 个，必须排除（沿用本项目静态断言一律 AST 的纪律）。
_callers20, _migration20 = [], []
for _dir20, _, _files20 in os.walk(os.path.join(ROOT, "app")):
    for _f20 in _files20:
        if not _f20.endswith(".py"):
            continue
        _p20 = os.path.join(_dir20, _f20)
        with open(_p20, encoding="utf-8") as _fh20:
            _text20 = _fh20.read()
        try:
            _tree20 = ast.parse(_text20)
        except SyntaxError:
            continue
        _rel20 = os.path.relpath(_p20, ROOT)
        for _n20 in _tree20.body:
            if isinstance(_n20, ast.FunctionDef) and _n20.name.startswith("ensure_"):
                _src20 = ast.get_source_segment(_text20, _n20) or ""
                _called20 = any(
                    isinstance(_c20, ast.Call) and isinstance(_c20.func, ast.Name)
                    and _c20.func.id == "ensure_registered_tables"
                    for _c20 in ast.walk(_n20))
                if _called20:
                    _callers20.append((_rel20, _n20.name))
                if "PRAGMA" in _src20.upper() or "ALTER TABLE" in _src20.upper():
                    _migration20.append((_n20.name, _called20))
check("20 个纯建表 ensure_* 全部接入 helper（AST 真实调用计数）",
      len(_callers20) == 20, str(len(_callers20)))
check("20 个调用点签名与函数名不变（无参 + -> None）",
      all("def " + name + "() -> None:" in read(p) for p, name in _callers20))
check("带 PRAGMA / ALTER 的迁移型 ensure_* 一行未动",
      len(_migration20) >= 4 and all(not called for _, called in _migration20),
      str([n for n, _ in _migration20]))
check("B3-B 等价性回归第 5 节已落地（旧骨架 oracle 逐事件比对）",
      "== 5. B3-B 纯建表样板抽离" in read("tests/test_b_batch_equivalence.py")
      and "def old_bootstrap(" in read("tests/test_b_batch_equivalence.py")
      and "DDL 顺序 / 连接 / cursor / commit 逐事件一致"
      in read("tests/test_b_batch_equivalence.py"))

print("== 问题 21: 梯队 0 治理能力（零运行时代码改动） ==")
import json as _json21

_t0_run_all = read("tools/run_all_checks.py")
_t0_arch = read("tools/architecture_report.py")
_t0_bench = read("tools/bench_hotpaths.py")
_t0_prop = read("tests/test_sanitizer_properties.py")
_t0_files = ("tools/run_all_checks.py", "tools/architecture_report.py",
             "tools/bench_hotpaths.py", "tests/test_sanitizer_properties.py")


def _t0_imports(src):
    """AST 取顶层及函数内 import 的根模块名；避开裸子串自匹配误报。"""
    out = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            out.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module.split(".")[0])
    return out


def _t0_literal(src, name):
    """AST 读取模块级 ``name = (...const...)`` / ``frozenset({...})`` 字面量。"""
    for n in ast.parse(src).body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in n.targets):
            val = n.value
            if isinstance(val, ast.Call) and val.args and isinstance(val.args[0], (ast.Set, ast.List, ast.Tuple)):
                val = val.args[0]
            if isinstance(val, (ast.Tuple, ast.List, ast.Set)):
                return [e.value for e in val.elts if isinstance(e, ast.Constant)]
    return []


check("梯队 0 四个脚本/测试全部落地且只用标准库（AST import 白名单）",
      all(os.path.isfile(os.path.join(ROOT, p)) for p in _t0_files)
      and all(_t0_imports(read(p)) <= {
          "__future__", "os", "sys", "json", "re", "ast", "glob", "time",
          "subprocess", "sqlite3", "importlib",
      } for p in _t0_files))
_t0_excluded = _t0_literal(_t0_run_all, "EXCLUDED")
check("全量校验网自动发现 + 排除自身 + 29 项显式排除 + 超时保护",
      all(s in _t0_run_all for s in (
          "def discover(", 'SELF = "tools/run_all_checks.py"',
          "TIMEOUT = 90", "PYTHONDONTWRITEBYTECODE"))
      and len(_t0_excluded) == 29
      and all(isinstance(p, str) and p.startswith("tests/") for p in _t0_excluded),
      "excluded=%d" % len(_t0_excluded))
_t0_gmax = _t0_literal(_t0_arch, "GATES_MAX")
_t0_gmin = _t0_literal(_t0_arch, "GATES_MIN")
check("架构门槛只 gate 5 项坏味道 + 1 项防回退（规模类不 gate）",
      set(_t0_gmax) == {"inline_script_lines", "inline_script_blocks",
                        "loop_db_calls", "loop_literal_regex_calls",
                        "longest_function_lines"}
      and _t0_gmin == ["ddl_helper_calls"],
      "max=%s min=%s" % (_t0_gmax, _t0_gmin))
check("架构 / 基准两份基线已入库且可用 --init 重建",
      all(os.path.isfile(os.path.join(ROOT, p)) for p in (
          "tools/architecture_baseline.json", "tools/bench_baseline.json"))
      and "--init" in _t0_arch and "--init" in _t0_bench)
check("基准脚本含 4 条确定性性能契约 + 归一化比值门槛",
      all(s in _t0_bench for s in (
          "deterministic_contracts", "RATIO_TOLERANCE", "REFERENCE",
          "1 次连接 / 1 次提交", "ns/op")))
_t0_golden = _json21.loads(read("tests/sanitizer_golden.json"))
check("净化 golden 覆盖 25 组输入 × 3 条通道（改代码前采集）",
      set(_t0_golden) == {"sanitize_html", "to_plain_text", "sanitize_rich_html"}
      and all(len(v) == 25 for v in _t0_golden.values()))
check("属性测试锁定三条反直觉事实与 B5 分层边界",
      all(s in _t0_prop for s in (
          "明确不幂等", "全部 25 组输入幂等", "保留纯文本节点里的危险字面量",
          "三通道输出互不相同", "Bark 实体乱码根治")))
check("梯队 0 四项能力全部接入 CI verify",
      all(s in ci for s in (
          "tests/test_sanitizer_properties.py",
          "tools/architecture_report.py .",
          "tools/bench_hotpaths.py .",
          "tools/run_all_checks.py .")))
check("2.5 发版：版本点已同步到 2.5（历史 tag 不重推）",
      'APP_VERSION = "2.5"' in read("app/shared/version.py")
      and "IMAGE_TAG:-2.5" in read("docker-compose.yml")
      and "request_app.js?v=2.5" in read("templates/request.html"))
check("重构文档新增 8.6 节记录梯队 0 与零运行时改动口径",
      "### 8.6" in read("docs/REFACTOR_2.0.md")
      and "零运行时代码改动" in read("docs/REFACTOR_2.0.md"))

print("== 问题 22: 2.1 批次 1（循环内字面量正则外提，可证等价） ==")
_t1_golden = _json21.loads(read("tests/loop_regex_golden.json"))
_t1_specs = _t1_golden["specs"]
_t1_files = sorted({s["file"] for s in _t1_specs})
check("改代码前采集的正则 golden 覆盖 16 组 spec / 7 个文件",
      len(_t1_specs) == 16 and len(_t1_files) == 7
      and all(set(s) >= {"id", "file", "op", "pattern", "flags", "inputs", "oracle"}
              for s in _t1_specs),
      "specs=%d files=%d" % (len(_t1_specs), len(_t1_files)))
_t1_consts = 0
_t1_loop_left = []
for _rel22 in _t1_files:
    _tr22 = ast.parse(read(_rel22))
    for _n22 in _tr22.body:
        if (isinstance(_n22, ast.Assign) and isinstance(_n22.value, ast.Call)
                and isinstance(_n22.value.func, ast.Attribute)
                and _n22.value.func.attr == "compile"
                and isinstance(_n22.value.func.value, ast.Name)
                and _n22.value.func.value.id == "re"):
            _t1_consts += 1
    for _n22 in ast.walk(_tr22):
        if isinstance(_n22, (ast.For, ast.AsyncFor, ast.While)):
            for _c22 in ast.walk(_n22):
                if (isinstance(_c22, ast.Call) and isinstance(_c22.func, ast.Attribute)
                        and isinstance(_c22.func.value, ast.Name)
                        and _c22.func.value.id == "re" and _c22.args
                        and isinstance(_c22.args[0], ast.Constant)
                        and isinstance(_c22.args[0].value, str)):
                    _t1_loop_left.append((_rel22, _c22.lineno))
check("16 个模块级 re.compile 常量落地（AST 赋值计数）",
      _t1_consts == 16, str(_t1_consts))
check("7 个文件循环体内字面量正则调用归零（AST 真实调用）",
      not _t1_loop_left, str(_t1_loop_left[:3]))
check("等价性测试以 golden + AST 双重锁定并锁定 21 个调用点",
      all(s in read("tests/test_loop_regex_equivalence.py") for s in (
          "loop_regex_golden.json", "CONST_BY_ID", "compile_consts",
          "循环体内已无字面量正则调用", "21 个调用点")))
check("2.1 等价性回归接入 CI verify",
      "tests/test_loop_regex_equivalence.py" in ci)
check("架构门槛新增 loop_literal_regex_calls 且基线为 0",
      "loop_literal_regex_calls" in read("tools/architecture_report.py")
      and _json21.loads(read("tools/architecture_baseline.json")).get("loop_literal_regex_calls") == 0)
check("2.1 未把既有 execute() 改成 execute_many（事务边界语义不变）",
      _json21.loads(read("tools/architecture_baseline.json")).get("execute_many_calls") == 0
      and "def execute(self, sql: str, params=()) -> int:" in read("app/infra/db/system_store.py"))
check("重构文档新增 2.1 章节记录完成口径与事务边界约束",
      "## 9. 2.1" in read("docs/REFACTOR_2.0.md")
      and "事务边界" in read("docs/REFACTOR_2.0.md"))

print("== 问题 23: 2.3 插件导入 / GitHub 发布窗口 / OpenAPI / 开发规范 ==")
import hashlib as _hashlib23
import json as _json23

_p23_ui = read("templates/plugins.html")
_p23_router = read("app/domains/plugins/router.py")
_p23_init = read("app/plugins/__init__.py")
_p23_main = read("app/main.py")
_p23_ci = read(".github/workflows/docker-publish.yml")
_p23_ext = read("app/plugins/external.py")
_p23_gh = read("app/plugins/github_source.py")

check("2.3 四个导入 / 注册表 / 卸载接口齐备",
      all(s in _p23_router for s in (
          '@router.get("/api/plugins/registry")',
          '@router.post("/api/plugins/import")',
          '@router.post("/api/plugins/import/github")',
          '@router.delete("/api/plugins/{plugin_id}/install"',
      )))
check("2.3 导入后强制禁用、卸载清理启用状态",
      'set_plugin_enabled(mf["id"], False)' in _p23_router
      and "delete_plugin_state(plugin_id)" in _p23_router)

# 用 AST 精确判定「导入不执行代码」：只看函数体内的真实调用
_p23_fns = {
    n.name: n for n in ast.parse(_p23_ext).body
    if isinstance(n, ast.FunctionDef)
}


def _p23_has_call(node, attr=None, name=None):
    for c in ast.walk(node):
        if not isinstance(c, ast.Call):
            continue
        if attr and isinstance(c.func, ast.Attribute) and c.func.attr == attr:
            return True
        if name and isinstance(c.func, ast.Name) and c.func.id == name:
            return True
    return False


check("2.3 install_from_zip 函数体内无任何模块执行（导入不执行代码）",
      "install_from_zip" in _p23_fns
      and not _p23_has_call(_p23_fns["install_from_zip"], attr="exec_module")
      and not _p23_has_call(_p23_fns["install_from_zip"], name="load_external"))
check("2.3 load_external 是唯一显式执行外部代码的入口",
      "load_external" in _p23_fns
      and _p23_has_call(_p23_fns["load_external"], attr="exec_module"))
check("2.3 加载器仅在显式启用或已启用恢复时载入外部代码",
      "external.load_external(plugin_id)" in _p23_init
      and "discover_external_plugins()" in _p23_init
      and "if pid not in enabled:" in _p23_init)

check("2.3 GitHub 来源：服务端拼 URL + 白名单 + 逐跳重定向 + 禁代理 + sha256",
      all(s in _p23_gh for s in (
          "def registry_url(", "def asset_url(", "def assert_safe_url(",
          "ALLOWED_HOSTS", "validate_repo(", "validate_asset_path(",
          "MAX_REDIRECTS", "ProxyHandler({})", "sha256_bytes(data)",
      )))
_p23_index = _json23.loads(read("plugins/index.json"))
check("2.3 官方索引 schema / repository 正确且条目强制 sha256",
      _p23_index.get("schema") == 1
      and _p23_index.get("repository") == "Abson2/emby-pulse"
      and bool(_p23_index.get("plugins"))
      and all(len(e.get("sha256", "")) == 64
              and e.get("download", "").endswith(".zip")
              for e in _p23_index["plugins"]))
for _e23 in _p23_index.get("plugins", []):
    _p23_asset = os.path.join(ROOT, _e23["download"])
    _p23_digest = ""
    if os.path.isfile(_p23_asset):
        with open(_p23_asset, "rb") as _fp23:
            _p23_digest = _hashlib23.sha256(_fp23.read()).hexdigest()
    check("2.3 官方产物 sha256 与索引一致: " + _e23["id"],
          bool(_p23_digest) and _p23_digest == _e23["sha256"])

check("2.3 OpenAPI 显式启用且文档页默认仅管理员",
      'openapi_url="/openapi.json"' in _p23_main
      and "configure_openapi_docs(app)" in _p23_main
      and "OPENAPI_PUBLIC" in read("app/core/openapi_guard.py")
      and "is_admin_request" in read("app/core/openapi_guard.py")
      and "OpenAPIGuardMiddleware" in read("app/bootstrap/middleware.py"))
check("2.3 CI 容器冒烟验证 OpenAPI 匿名不可读",
      "/openapi.json" in _p23_ci and "OpenAPI" in _p23_ci)
check("2.3 插件中心前端具备导入 / 卸载入口且沿用单一 nonce 脚本",
      'id="plugin-import-modal"' in _p23_ui
      and "uninstallExternalPlugin(" in _p23_ui
      and "loadGithubRegistry(" in _p23_ui
      and _p23_ui.count("<script") == 1)
check("2.3 远程索引字段统一转义、结果区用 textContent",
      "epEsc(item.name)" in _p23_ui
      and "epEsc(item.description)" in _p23_ui
      and "box.textContent = text" in _p23_ui)
check("2.3 开发规范 / AI 话术 / 示例插件 / 打包工具齐备",
      all(os.path.isfile(os.path.join(ROOT, p)) for p in (
          "docs/PLUGIN_DEVELOPMENT.md", "docs/PLUGIN_AI_PROMPT.md",
          "examples/hello_plugin/manifest.json", "examples/hello_plugin/plugin.py",
          "tools/pack_plugin.py", "plugins/README.md",
      )))
check("2.3 插件导入回归显式接入 CI verify",
      "tests/test_plugin_import.py" in _p23_ci)
check("2.3 打包工具自检可被全量网自动发现（无参数 rc=0）",
      "def self_check()" in read("tools/pack_plugin.py")
      and "PACK PLUGIN SELF CHECK PASSED" in read("tools/pack_plugin.py"))

print("== 问题 24: 2.4 安全加固（IP 认定 / 端口隔离 / 落盘权限 / token 吊销 / 出站 SSRF / 字段加密） ==")
rate_lim = read("app/core/rate_limiter.py")
auth = read("app/domains/users/auth.py")
auth_router = read("app/domains/media_requests/auth_router.py")
jwt = read("app/core/jwt_token.py")
main_py = read("app/main.py")
outbound = read("app/core/outbound_url.py")
uns = read("app/domains/users/user_notify_service.py")
unr = read("app/domains/users/user_notify_router.py")
ubd = read("app/domains/users/user_bot_dao.py")
pdao = read("app/plugins/plugin_dao.py")
secbox = read("app/core/secret_box.py")

check("A1 限速/锁定/审计 IP 优先采用不可伪造的 CF-Connecting-IP",
      "CF-Connecting-IP" in rate_lim
      and "X-Forwarded-For" in rate_lim)

check("A2 登录端口隔离改读 ASGI scope 不可伪造标记（防 Host/端口伪造）",
      "USER_PORTAL_SCOPE_FLAG" in auth
      and "USER_PORTAL_SCOPE_FLAG" in auth_router
      and "request.scope.get(USER_PORTAL_SCOPE_FLAG)" in auth
      and "request.scope.get(USER_PORTAL_SCOPE_FLAG) is not True" in auth_router)

check("A5 进程启动即收敛落盘权限（umask 0o077）",
      "os.umask(0o077)" in main_py)

check("A6 管理员令牌吊销 fail-closed（DB 异常时拒绝）",
      'if payload.get("is_admin"):' in jwt
      and "return None" in jwt)

check("A7 新建出站地址安全校验模块（拦链路本地/回环）",
      "def assert_safe_outbound(" in outbound
      and "169.254.0.0/16" in outbound
      and "127.0.0.0/8" in outbound)
check("A7 用户通知 Webhook 发送前再校验 + 禁重定向 opener",
      "assert_safe_outbound(url)" in uns
      and "_NoRedirect" in uns
      and "_opener.open" in uns)
check("A7 用户通知保存前校验 Bark/Webhook 出站地址",
      "assert_safe_outbound" in unr)

check("A3 Emby 口令在 user_bot_dao 边界透明加解密（零上层改动）",
      "secret_box.decrypt(row[\"init_password\"]" in ubd
      and "secret_box.encrypt(init_password)" in ubd)
check("A4 插件配置在 plugin_dao 边界透明加解密（含第三方凭证）",
      "secret_box.decrypt(row[\"config\"])" in pdao
      and "secret_box.encrypt(json.dumps(config" in pdao)
check("A3/A4 加解密模块基于已声明 pycryptodome 且明文前缀兼容",
      "from Crypto.Cipher import AES" in secbox
      and '_PREFIX = "enc:v1:"' in secbox
      and "startswith(_PREFIX)" in secbox)

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL HARDENING CHECKS PASSED")
