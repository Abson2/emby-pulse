#!/usr/bin/env python3
"""断言 5 项加固改动确实落在源码里，并对关键函数做真实行为测试。

只依赖标准库，不需要安装应用依赖，因此在 CI 里跑得快且稳定。
用法: python tools/verify_patches.py [ROOT]
"""
from __future__ import annotations

import ast
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

print("== 问题 3: 明文传送 token ==")
webhook = read("app/domains/system/webhook.py")
wns = exec_nodes(webhook, {"_get_webhook_token"})
os.environ.pop("WEBHOOK_ALLOW_QUERY_TOKEN", None)
q = FakeRequest("", {}, {"token": "QUERY_SECRET_VALUE"})
check("默认拒绝 URL 中的 token", wns["_get_webhook_token"](q) is None)
os.environ["WEBHOOK_ALLOW_QUERY_TOKEN"] = "1"
check("兼容开关显式开启才生效",
      wns["_get_webhook_token"](q) == "QUERY_SECRET_VALUE")
os.environ.pop("WEBHOOK_ALLOW_QUERY_TOKEN", None)
h = FakeRequest("", {"X-Webhook-Token": "HEADER_SECRET"}, {"token": "QUERY_SECRET_VALUE"})
check("请求头优先", wns["_get_webhook_token"](h) == "HEADER_SECRET")
check("Dockerfile 默认关闭 query token", "WEBHOOK_ALLOW_QUERY_TOKEN=0" in dockerfile)

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

print("== 问题 5: F12 泄露后台配置 ==")
bot = read("app/bot/bot.py")
tpl = read("templates/settings.html")
check("后端不再拼 token 到 webhook_url", "/api/v1/webhook?token=" not in bot)
check("后端不再直接回传明文 webhook_token",
      'all_config["webhook_token"] = webhook_token' not in bot)
check("模板不再把 token 拼进 URL", "webhook?token=" not in tpl)
check("模板 URL 预览为纯路径", "${host}/api/v1/webhook`" in tpl)

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

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL HARDENING CHECKS PASSED")
