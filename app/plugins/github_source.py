# -*- coding: utf-8 -*-
"""本项目 GitHub 插件发布窗口（2.3）：索引读取 + 产物下载。

发布约定（见 ``plugins/README.md``）：

* 官方插件索引固定在仓库 ``plugins/index.json``；
* 每个条目给出仓库内相对路径的 zip 产物 ``download`` 与 ``sha256``；
* 未来新增插件只需往仓库提交索引条目与 zip，**无需改动主体代码**。

SSRF / 供应链防护：

* URL 一律由服务端按 ``repo`` / ``ref`` / ``download`` 拼接，不接受调用方传入任意 URL；
* repo 白名单默认只含本项目，可用 ``PLUGIN_GITHUB_REPOS`` 追加；
* ref / 路径严格正则，禁止 ``..``、协议、盘符、反斜杠；
* 目标主机白名单 + 仅 HTTPS + 无认证信息 + 端口 443；
* 逐跳校验重定向（最多 3 跳），并用空 ProxyHandler 禁用环境代理；
* 显式超时、索引/产物大小上限、产物强制 sha256 校验。

本模块只依赖标准库，便于纯标准库契约测试用 ``runpy.run_path`` 独立加载。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_REPO = "Abson2/emby-pulse"
DEFAULT_REF = "main"
REGISTRY_PATH = "plugins/index.json"

ALLOWED_HOSTS = frozenset({
    "raw.githubusercontent.com",
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
})

MAX_REGISTRY_BYTES = 512 * 1024
MAX_ASSET_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT = 15

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,64}$")
REF_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PluginSourceError(ValueError):
    """GitHub 源读取失败。消息可直接回显给管理员。"""


def allowed_repos() -> frozenset:
    repos = {DEFAULT_REPO}
    extra = os.environ.get("PLUGIN_GITHUB_REPOS", "").strip()
    for item in extra.split(","):
        item = item.strip()
        if item and REPO_RE.match(item):
            repos.add(item)
    return frozenset(repos)


def current_ref(ref: str | None = None) -> str:
    value = (ref or os.environ.get("PLUGIN_GITHUB_REF", "") or DEFAULT_REF).strip()
    if not REF_RE.match(value) or value in (".", ".."):
        raise PluginSourceError("ref 不合法")
    return value


def validate_repo(repo: str) -> str:
    if not isinstance(repo, str) or not REPO_RE.match(repo) or ".." in repo:
        raise PluginSourceError("仓库名不合法")
    if repo not in allowed_repos():
        raise PluginSourceError("仓库不在允许清单中: %s" % repo)
    return repo


def validate_asset_path(path: str) -> str:
    if not isinstance(path, str) or not path or len(path) > 200:
        raise PluginSourceError("download 不合法")
    if "\\" in path or ".." in path.split("/") or "://" in path or path.startswith("/"):
        raise PluginSourceError("download 不合法")
    if any(part in ("", ".") for part in path.split("/")) or not path.lower().endswith(".zip"):
        raise PluginSourceError("download 必须是仓库内 .zip 相对路径")
    return path


def registry_url(repo: str, ref: str) -> str:
    return "https://raw.githubusercontent.com/%s/%s/%s" % (
        validate_repo(repo), current_ref(ref), REGISTRY_PATH,
    )


def asset_url(repo: str, ref: str, path: str) -> str:
    return "https://raw.githubusercontent.com/%s/%s/%s" % (
        validate_repo(repo), current_ref(ref), validate_asset_path(path),
    )


def assert_safe_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS:
        raise PluginSourceError("目标地址不在 GitHub 白名单内")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise PluginSourceError("目标地址不合法")
    if "\x00" in url or "\n" in url or "\r" in url:
        raise PluginSourceError("目标地址含非法字符")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动重定向，改为逐跳白名单校验。"""

    def redirect_request(self, *args, **kwargs):
        return None


def _read_limited(resp, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = resp.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise PluginSourceError("响应超过 %d 字节上限" % limit)
        chunks.append(chunk)
    return b"".join(chunks)


def http_get(url: str, limit: int, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """白名单 HTTPS GET；手动跟随重定向并逐跳校验。"""
    current = url
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    for _ in range(MAX_REDIRECTS + 1):
        assert_safe_url(current)
        req = urllib.request.Request(
            current,
            headers={"User-Agent": "EmbyPulse-PluginHub/2.3", "Accept": "*/*"},
            method="GET",
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                return _read_limited(resp, limit)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code in (301, 302, 303, 307, 308) and location:
                current = urllib.parse.urljoin(current, location)
                continue
            raise PluginSourceError("GitHub 返回 HTTP %d" % exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise PluginSourceError("网络请求失败或超时") from exc
    raise PluginSourceError("重定向次数过多")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_registry(obj, repo: str) -> dict:
    if not isinstance(obj, dict) or obj.get("schema") != 1:
        raise PluginSourceError("索引 schema 必须为 1")
    if obj.get("repository") != repo:
        raise PluginSourceError("索引 repository 与请求的仓库不一致")
    entries = obj.get("plugins")
    if not isinstance(entries, list) or len(entries) > 200:
        raise PluginSourceError("索引 plugins 必须是数组且不超过 200 项")
    result = []
    for item in entries:
        if not isinstance(item, dict):
            raise PluginSourceError("索引条目必须是对象")
        pid = item.get("id")
        if not isinstance(pid, str) or not ID_RE.match(pid):
            raise PluginSourceError("索引条目 id 不合法")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.match(digest.lower()):
            raise PluginSourceError("索引条目必须提供 sha256")
        result.append({
            "id": pid,
            "name": str(item.get("name", pid))[:80],
            "version": str(item.get("version", ""))[:32],
            "description": str(item.get("description", ""))[:300],
            "download": validate_asset_path(item.get("download")),
            "sha256": digest.lower(),
            "min_app_version": str(item.get("min_app_version", ""))[:32],
        })
    return {"schema": 1, "repository": repo, "plugins": result}


def fetch_registry(repo: str = DEFAULT_REPO, ref: str | None = None) -> dict:
    repo = validate_repo(repo)
    ref = current_ref(ref)
    raw = http_get(registry_url(repo, ref), MAX_REGISTRY_BYTES, DEFAULT_TIMEOUT)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise PluginSourceError("索引不是有效 UTF-8 JSON") from exc
    return validate_registry(obj, repo)


def fetch_asset(entry: dict, repo: str = DEFAULT_REPO, ref: str | None = None) -> bytes:
    repo = validate_repo(repo)
    ref = current_ref(ref)
    data = http_get(asset_url(repo, ref, entry["download"]), MAX_ASSET_BYTES, 30)
    if sha256_bytes(data) != entry["sha256"]:
        raise PluginSourceError("产物 sha256 校验失败")
    return data


def find_entry(registry: dict, plugin_id: str) -> dict:
    if not isinstance(plugin_id, str) or not ID_RE.match(plugin_id):
        raise PluginSourceError("插件 id 不合法")
    for entry in registry.get("plugins", []):
        if entry["id"] == plugin_id:
            return entry
    raise PluginSourceError("官方索引中不存在插件: %s" % plugin_id)


def install_from_github(plugin_id: str, ref: str | None = None,
                        repo: str = DEFAULT_REPO, overwrite: bool = False) -> dict:
    """拉取官方产物并复用本地 zip 校验链安装；仍需管理员显式启用。"""
    # 延迟导入：保持本模块可被纯标准库测试独立加载
    from app.plugins import external

    repo = validate_repo(repo)
    ref = current_ref(ref)
    registry = fetch_registry(repo, ref)
    entry = find_entry(registry, plugin_id)
    data = fetch_asset(entry, repo, ref)
    origin = {
        "type": "github",
        "repo": repo,
        "ref": ref,
        "asset": entry["download"],
    }
    return external.install_from_zip(data, origin=origin, overwrite=overwrite)
