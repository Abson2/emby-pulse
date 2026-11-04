#!/usr/bin/env python3
"""
入库前凭证 / 个人配置扫描。

用法: python tools/check-no-secrets.py [ROOT]   (ROOT 默认为当前目录)
退出码: 0 = 干净, 1 = 命中, 2 = 用法错误

策略：
  * 全仓检查：凭证前缀、私钥头、SQLite/二进制、禁入库文件名、
    配置数据中的非空凭证值、以及个人门户配置键的字面量赋值。
  * 交付/部署文件额外检查：凭证字面量赋值、具体内网主机地址、非示例域名。
    源码里出现的第三方 API 域名属正常，不做域名检查以免误报。
报错只输出文件路径与规则名，绝不回显命中内容，避免扫描日志成为新泄露面。
"""
from __future__ import annotations

import os
import re
import sys

SELF = os.path.basename(__file__)
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".trellis"}
SCAN_EXT = {
    ".md", ".txt", ".yml", ".yaml", ".json", ".example", ".py", ".sh",
    ".env", ".toml", ".ini", ".cfg", ".conf", ".html", ".js", ".css",
    ".lock", "",  # 无扩展名文件（Dockerfile / .gitignore 等）
}
# 这些文件/目录不做域名与具体 IP 检查（第三方依赖地址或应用源码）
DOMAIN_EXEMPT_TOP = {"app", "templates", "static", "tests"}
DOMAIN_EXEMPT_FILES = {"uv.lock", "requirements.txt", "pyproject.toml",
                       "CHANGELOG.md", "LICENSE", "LICENSE.txt"}

DOMAIN_ALLOW = {
    "example.com", "example.org", "example.net", "localhost",
    "github.com", "githubusercontent.com", "github.io", "ghcr.io",
    "docker.io", "python.org", "pypi.org", "pythonhosted.org",
    "readthedocs.io", "ietf.org", "rfc-editor.org", "apache.org",
    "astral.sh", "ubuntu.com", "debian.org",
    # 用户明确引用的公开项目（Cloudflare Tunnel 内网穿透介绍），非个人配置
    "naspk.com",
}
TLD = {
    "com", "org", "net", "io", "cn", "cc", "cd", "dev", "app", "me",
    "tv", "co", "xyz", "top", "site", "online", "cloud", "hk", "tw",
    "uk", "de", "jp", "ru", "info", "biz", "pro", "tech", "fun", "vip",
    "icu", "work", "link", "click", "live", "shop", "wiki", "space",
    "website", "press", "host", "pw", "su", "us", "ca", "au", "fr",
    "nl", "kr", "sg", "in", "eu", "la", "ws", "cx", "mom", "sh",
    "cyou", "sbs", "beauty", "lol", "zip", "mov", "art", "blog", "cash",
}
DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+([a-z]{2,})\b", re.I)

# 具体内网主机地址；CIDR 写法（后跟 /）属正常配置项，不命中
PRIVATE_IP_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b(?!\s*/)"
)

SECRET_ASSIGN_RE = re.compile(
    r"(?m)^[ \t]*[A-Za-z0-9_]*(?:TOKEN|API_KEY|APIKEY|SECRET|PASSWORD|PASSWD|"
    r"PRIVATE_KEY|ACCESS_KEY|SESSION_KEY)[A-Za-z0-9_]*[ \t]*=[ \t]*([^\r\n#]*)"
)
# 个人门户配置键（config.json 里的字段）被写进任何文件时都要能抓到
PERSONAL_KEY_RE = re.compile(
    r"(?m)^[ \t]*(?:emby_api_key|emby_public_url|user_portal_url|webhook_base_url|"
    r"tg_bot_token|tmdb_api_key|webhook_token|secret_key)[ \t]*[:=][ \t]*"
    r"[\"']([^\"']{8,})[\"']"
)
CONFIG_VALUE_RE = re.compile(
    r'"(?:emby_api_key|webhook_token|tg_bot_token|tmdb_api_key|secret_key|'
    r'api_key|password|access_token|emby_public_url|user_portal_url)"'
    r'\s*:\s*"([^"]{4,})"'
)

# 拼接构造，避免本文件自身包含完整凭证前缀
TOKEN_PREFIXES = [
    "gh" + "p_", "gh" + "o_", "gh" + "u_", "gh" + "s_", "gh" + "r_",
    "github" + "_pat_", "xox" + "b-", "AKIA",
]
PRIVATE_KEY_HEADS = [
    "-----BEGIN " + p for p in
    ("RSA PRIVATE KEY", "OPENSSH PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY")
]
SQLITE_MAGIC = b"SQLite format 3\x00"
BAD_FILENAME = re.compile(
    r"(^|/)(?:config\.json|.*\.db|.*\.sqlite3?|.*\.bak.*|.*\.dump|"
    r".*\.pem|.*\.key|id_rsa.*|credentials.*|\.env(?!\.example))$", re.I
)
PLACEHOLDER = re.compile(
    r"(^\s*$|^\s*[<$]|YOUR_|CHANGE_?ME|PLACEHOLDER|EXAMPLE|<[^>]+>|"
    r"^\$\{|^[01]$|^true$|^false$|^null$|^none$|^off$|^enforce$|"
    r"^\s*[\"']?\s*[\"']?$)", re.I
)
# 全仓范围下，环境变量名、测试 fixture、示例值都不算凭证。
# 只有"足够长且不像占位符"的字面量才判为疑似真实凭证。
VARNAME_OR_FIXTURE = re.compile(
    r"(^[A-Z][A-Z0-9_]*$"
    r"|example|test|fake|dummy|placeholder|sample|mock|your_|xxx|"
    r"changeme|redacted|\$\{|<)",
    re.I,
)


def looks_like_secret(v: str, strict_scope: bool) -> bool:
    if PLACEHOLDER.search(v) or VARNAME_OR_FIXTURE.search(v):
        return False
    return len(v) >= (4 if strict_scope else 16)


def allowed_domain(host: str) -> bool:
    h = host.lower().strip(".")
    return any(h == a or h.endswith("." + a) for a in DOMAIN_ALLOW)


def delivery_scope(rel: str) -> bool:
    top = rel.split("/")[0]
    if top in DOMAIN_EXEMPT_TOP:
        return False
    if rel in DOMAIN_EXEMPT_FILES:
        return False
    return True


def findings_for(rel: str, data: bytes) -> list[str]:
    out: list[str] = []
    delivery = delivery_scope(rel)

    if BAD_FILENAME.search(rel):
        out.append("禁止入库的配置/数据/密钥文件名")
    if data.startswith(SQLITE_MAGIC):
        out.append("SQLite 数据库文件")
    if b"\x00" in data[:4096]:
        # 图片/字体等二进制资源在 static/ 下，只按扩展名排除
        if not rel.startswith("static/"):
            out.append("疑似二进制数据文件")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return out

    for p in TOKEN_PREFIXES:
        if p in text:
            out.append("凭证前缀")
            break
    for p in PRIVATE_KEY_HEADS:
        if p in text:
            out.append("私钥 PEM 头")
            break

    for m in CONFIG_VALUE_RE.finditer(text):
        if looks_like_secret(m.group(1), delivery):
            out.append("配置数据含非空凭证 L%d" % (text[:m.start()].count("\n") + 1))
            break

    for m in PERSONAL_KEY_RE.finditer(text):
        if looks_like_secret(m.group(1), delivery):
            out.append("个人门户配置字面量 L%d" % (text[:m.start()].count("\n") + 1))
            break

    if delivery:
        for m in SECRET_ASSIGN_RE.finditer(text):
            v = m.group(1).strip().strip("\"'").strip()
            if not PLACEHOLDER.search(v) and len(v) >= 8:
                out.append("疑似凭证赋值 L%d" % (text[:m.start()].count("\n") + 1))

        for m in DOMAIN_RE.finditer(text):
            if m.group(1).lower() not in TLD:
                continue
            if allowed_domain(m.group(0)):
                continue
            out.append("非示例域名 L%d" % (text[:m.start()].count("\n") + 1))

        for m in PRIVATE_IP_RE.finditer(text):
            out.append("具体内网主机地址 L%d" % (text[:m.start()].count("\n") + 1))

    return out


def main() -> int:
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    if not os.path.isdir(root):
        print("路径不存在: " + root)
        return 2
    hits: list[tuple[str, str]] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn == SELF:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SCAN_EXT:
                continue
            try:
                data = open(full, "rb").read()
            except OSError:
                continue
            scanned += 1
            for rule in findings_for(rel, data):
                hits.append((rel, rule))

    print("scanned files: %d" % scanned)
    if hits:
        print("FINDINGS: %d" % len(hits))
        for rel, rule in sorted(set(hits)):
            # 只输出文件与规则，绝不回显命中的原始内容
            print("  [!] %s  ->  %s" % (rel, rule))
        return 1
    print("FINDINGS: 0  (no credentials / personal config)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
