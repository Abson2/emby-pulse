#!/usr/bin/env python3
"""2.0 安全契约静态审查（纯标准库，可直接 python 运行）。

目的：把「系统安全性审查」的结论固化为 CI 可执行的断言，防止后续迭代
（重构 / 抽离 / 合并）无意中削弱既有防护。本脚本**只读源码**，不改任何
运行时行为，属于纯加法。

覆盖：CSRF / AuthGate / 会话 Cookie 隔离 / CSP nonce / SSRF 代理校验 /
路径穿越 / 敏感字段 env 中央注册 / 富文本净化 / 纯文本通知入口 / 凭证扫描。

用法: python tools/check_security_contracts.py [ROOT]
"""
from __future__ import annotations

import importlib.util
import os
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


def exists(rel: str) -> bool:
    return os.path.isfile(os.path.join(ROOT, rel))


csrf = read("app/core/csrf_middleware.py")
gate = read("app/core/auth_gate_middleware.py")
mw = read("app/bootstrap/middleware.py")
sess = read("app/core/session.py")
smw = read("app/core/session_middleware.py")
portal = read("app/bootstrap/user_portal.py")
csp = read("app/core/security_headers_middleware.py")
proxy = read("app/utils/proxy_helper.py")
pwa = read("app/domains/pwa/router.py")
dbm = read("app/infra/db/db_manager.py")
cfg = read("app/core/config.py")
secu = read("app/core/security_utils.py")
ann = read("app/domains/notifications/announcements_router.py")
msg = read("app/domains/notifications/messages.py")
ci = read(".github/workflows/docker-publish.yml")
dockerfile = read("Dockerfile")
compose = read("docker-compose.yml")
auth = read("app/domains/users/auth.py")
auth_router = read("app/domains/media_requests/auth_router.py")

print("== 1. CSRF / 反向代理头 ==")
check("CSRF 定义 Origin 期望值计算", "_expected_origins" in csrf)
check("CSRF 定义同源判定", "_origin_allowed" in csrf and "_same_origin" in csrf)
check("CSRF 兼容代理头（Cloudflare Tunnel）", "x-forwarded-proto" in csrf)
check("compose / Dockerfile 默认 FORWARDED_ALLOW_IPS",
      "FORWARDED_ALLOW_IPS" in dockerfile and "FORWARDED_ALLOW_IPS" in compose)

print("== 2. 默认拒绝鉴权网关 ==")
check("AuthGate 定义默认匿名白名单", "DEFAULT_PUBLIC_API_PATHS" in gate)
check("AuthGate 区分管理员前缀", "ADMIN_ONLY_PREFIXES" in gate and "def is_admin_api" in gate)
check("AuthGate 已注册且位于会话中间件内层",
      "app.add_middleware(AuthGateMiddleware)" in mw
      and mw.index("app.add_middleware(AuthGateMiddleware)")
          < mw.index("app.add_middleware(DatabaseSessionMiddleware)"))

print("== 3. 10307 / 10308 会话 Cookie 隔离 ==")
check("定义两个独立 Cookie 名",
      "SESSION_COOKIE_NAME" in sess and "USER_PORTAL_SESSION_COOKIE_NAME" in sess)
check("中间件按请求动态取 Cookie 名", "session_cookie_name(request.scope)" in smw)
check("读写 Cookie 均用动态名",
      "request.cookies.get(cookie_name)" in smw and "key=cookie_name" in smw)
check("用户端 wrapper 在进入主 app 前打 scope 标记",
      "scope[USER_PORTAL_SCOPE_FLAG] = True" in portal
      and portal.index("scope[USER_PORTAL_SCOPE_FLAG] = True")
          < portal.index("await app(scope, receive, send)"))
check("端口隔离两端均读不可伪造的 scope 标记（防 Host/端口伪造）",
      "USER_PORTAL_SCOPE_FLAG" in auth
      and "request.scope.get(USER_PORTAL_SCOPE_FLAG) is True" in auth
      and "USER_PORTAL_SCOPE_FLAG" in auth_router
      and "request.scope.get(USER_PORTAL_SCOPE_FLAG) is not True" in auth_router)

print("== 4. CSP / 安全响应头 ==")
check("CSP 使用 nonce 化 script-src-elem",
      "script-src-elem" in csp and "nonce-{nonce}" in csp)
check("CSP 限制 frame-ancestors 为 self", "frame-ancestors 'self'" in csp)
check("请求级注入 csp_nonce", "request.state.csp_nonce" in csp)
check("内联脚本模板带 nonce（抽样）",
      any('nonce="{{ request.state.csp_nonce }}"' in read(t)
          for t in ("templates/request.html", "templates/register.html",
                    "templates/messages.html")))

print("== 5. SSRF 代理校验 ==")
check("代理读取层做运行期校验", "validate_proxy_url" in proxy)
check("非法代理自动降级为不使用代理",
      "_proxies_cache[raw] = None" in proxy and "return None" in proxy)
check("提供缓存失效钩子（配置变更后重新校验）", "def invalidate_cache()" in proxy)
check("存在独立 URL 校验模块", exists("app/utils/url_validator.py"))

print("== 6. 路径穿越防护 ==")
check("PWA 图标读取拒绝 .. 与路径分隔符",
      '".." in filename' in pwa and '"/" in filename' in pwa)
check("PWA 图标读取二次校验 abspath 前缀", "filepath.startswith(base_dir)" in pwa)
check("备份删除拒绝 .. 与路径分隔符", '".." in filename' in dbm)
check("备份删除二次校验 realpath 前缀",
      "real_path.startswith(real_backup_dir + os.sep)" in dbm)

print("== 7. 敏感字段 env 中央注册 ==")
check("存在 SENSITIVE_ENV_FIELDS 中央注册表", "SENSITIVE_ENV_FIELDS" in cfg)
check("Webhook / Bark / TG / Emby 等关键凭证均在册",
      all(k in cfg for k in (
          '"webhook_token": "WEBHOOK_TOKEN"',
          '"bark_device_key": "BARK_DEVICE_KEY"',
          '"tg_bot_token": "TG_BOT_TOKEN"',
          '"emby_api_key": "EMBY_API_KEY"')))
check("提供 env 来源判定", "def get_env_source(" in cfg)
check("启动日志不打印 emby_host 明文",
      "emby_host: {self.config.get('emby_host', '未设置')}" not in cfg)
check("Webhook 自动 Token 不在日志输出片段",
      "new_token[:8]" not in read("app/core/security_check.py"))

print("== 8. 富文本净化 / 纯文本通道 ==")
check("富文本采用标签白名单净化（零第三方依赖）",
      "def sanitize_rich_html(" in secu and "_RICH_ALLOWED_TAGS" in secu
      and "import bleach" not in secu)
check("危险模式清单单点定义（不重复）",
      "_DANGEROUS_PATTERNS" in secu and "def to_plain_text(" in secu)
check("兼容历史二次转义数据", "def decode_announcement_content(" in secu)
check("公告用户级通知不再用 sanitize_html 转义",
      "to_plain_text(data.content, max_length=2000)" in ann)
check("管理员消息用户级通知不再用 sanitize_html 转义",
      "to_plain_text(data.content, max_length=5000)" in msg)
check("网页端存储仍保留 sanitize_html / sanitize_rich_html（不削弱 XSS 防护）",
      "insert_admin_message(" in msg
      and "sanitize_html(data.content, max_length=5000)" in msg
      and "sanitize_rich_html(data.content, max_length=50000)" in ann)

check("S5 style 属性危险 CSS 值被清洗（合法声明不变）",
      "_RICH_UNSAFE_STYLE" in secu and "url\\s*\\([^)]*\\)" in secu)
check("S6 表名 f-string 拼接走集中注册表（非用户输入，防御性）",
      "TABLE_SCHEMAS" in read("app/infra/db/schema_registry.py")
      and "def ensure_registered_table(" in read("app/infra/db/schema_bootstrap.py"))

print("== 9. 行为级净化断言（直接执行 security_utils） ==")
spec = importlib.util.spec_from_file_location(
    "ep_sec_contract", os.path.join(ROOT, "app/core/security_utils.py"))
secu_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(secu_mod)
check("script 标签被剥离",
      "<script" not in secu_mod.sanitize_rich_html("<script>alert(1)</script><p>OK</p>")
      and "OK" in secu_mod.sanitize_rich_html("<script>alert(1)</script><p>OK</p>"))
check("javascript: 协议被剥离",
      "javascript:" not in secu_mod.sanitize_rich_html('<a href="javascript:alert(1)">x</a>'))
check("事件属性被剥离",
      "onerror" not in secu_mod.sanitize_rich_html('<img src="x" onerror="alert(1)">'))
check("to_plain_text 不产生实体乱码",
      "&lt;" not in secu_mod.to_plain_text("<p>测试</p>")
      and secu_mod.to_plain_text("<p>测试</p>") == "测试")
check("to_plain_text 对纯文本幂等",
      secu_mod.to_plain_text("普通消息 A & B") == "普通消息 A & B")

print("== 10. CI 防护链 ==")
check("凭证扫描器存在且接入 CI",
      exists("tools/check-no-secrets.py") and "tools/check-no-secrets.py" in ci)
check("会话隔离 / 加固断言 / 等价性回归均接入 CI",
      all(s in ci for s in (
          "tools/check_session_cookie_isolation.py",
          "tools/verify_patches.py",
          "tests/test_refactor_2_0_equivalence.py")))
check("富文本净化回归接入 CI", "tests/test_sanitize_rich_html.py" in ci)

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("SECURITY CONTRACT CHECKS PASSED")
