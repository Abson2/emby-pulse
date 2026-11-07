# -*- coding: utf-8 -*-
"""外部插件包管理（2.3）：校验 / 安装 / 卸载 / 按需加载。

安全边界（本功能是「引入可执行代码」的入口，必须逐条成立）：

* 外部插件统一落在持久化目录 ``PLUGIN_EXTERNAL_DIR``（默认 ``/workspace/data/plugins``，
  对应 compose 的 ``./data`` 卷），不写进镜像内的 ``app/plugins/``；
* **导入只落盘 + 记录来源，默认禁用，绝不在此阶段执行外部代码**；
* 只有管理员显式启用时，才 ``import`` 插件的 ``plugin.py``；
* 禁止覆盖 / 冒充内置插件；禁止路径穿越、符号链接、危险扩展名、zip bomb；
* 元数据文本字段拒绝 HTML 特殊字符，避免插件中心模板注入；
* ``origin.json`` 记录来源（local / github / repo / ref / sha256 / imported_at），可审计。

本模块只依赖标准库，便于纯标准库契约测试用 ``runpy.run_path`` 独立加载；
``PluginBase`` 仅在 ``load_external()`` 内延迟导入。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import uuid
import zipfile
from datetime import datetime

DEFAULT_EXTERNAL_DIR = "/workspace/data/plugins"

MAX_ZIP_BYTES = 5 * 1024 * 1024            # 上传包上限 5 MiB
MAX_UNCOMPRESSED_BYTES = 10 * 1024 * 1024  # 解压后总大小上限 10 MiB
MAX_SINGLE_FILE_BYTES = 2 * 1024 * 1024    # 单文件上限 2 MiB
MAX_FILES = 200
MAX_COMPRESSION_RATIO = 200                 # 单文件压缩比上限，防 zip bomb

ALLOWED_EXTENSIONS = frozenset({
    ".py", ".json", ".md", ".txt", ".html", ".css", ".js", ".svg",
    ".png", ".jpg", ".jpeg", ".webp", ".ico", ".yaml", ".yml", ".ini", ".cfg",
})

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
REF_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TEXT_FORBIDDEN = frozenset("<>&\"'`\\\x00")
SAFE_CLASS_RE = re.compile(r"^[A-Za-z0-9 _-]{0,96}$")

# 与加载器 / DAO 同目录的保留名，禁止外部插件占用
RESERVED_NAMES = frozenset({
    "base", "__init__", "plugin_dao", "external", "github_source", "__pycache__",
})

MANIFEST_REQUIRED = ("manifest_version", "id", "name", "version", "entry")


class PluginPackageError(ValueError):
    """插件包校验失败。消息可直接回显给管理员，不含敏感信息。"""


def external_dir() -> str:
    path = os.environ.get("PLUGIN_EXTERNAL_DIR", "").strip() or DEFAULT_EXTERNAL_DIR
    return os.path.abspath(path)


def builtin_ids() -> frozenset:
    """内置插件目录名 + 保留名。外部插件不得使用这些 id。"""
    base = os.path.dirname(os.path.abspath(__file__))
    names = set(RESERVED_NAMES)
    try:
        for name in os.listdir(base):
            if os.path.isdir(os.path.join(base, name)) and not name.startswith("."):
                names.add(name)
    except OSError:
        pass
    return frozenset(names)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_text(value, field: str, max_len: int = 200) -> str:
    """元数据文本：拒绝 HTML 特殊字符与控制字符，避免模板注入。"""
    if not isinstance(value, str):
        raise PluginPackageError("%s 必须是字符串" % field)
    if len(value) > max_len:
        raise PluginPackageError("%s 超过 %d 字符上限" % (field, max_len))
    for ch in value:
        if ch in TEXT_FORBIDDEN or ord(ch) < 32:
            raise PluginPackageError("%s 含不允许的字符" % field)
    return value


def validate_manifest(raw, reserved: frozenset | None = None) -> dict:
    if not isinstance(raw, dict):
        raise PluginPackageError("manifest.json 必须是 JSON 对象")
    for key in MANIFEST_REQUIRED:
        if key not in raw:
            raise PluginPackageError("manifest.json 缺少必填字段 %s" % key)
    if raw.get("manifest_version") != 1:
        raise PluginPackageError("manifest_version 必须为 1")

    pid = raw.get("id")
    if not isinstance(pid, str) or not ID_RE.match(pid):
        raise PluginPackageError("id 必须匹配 ^[a-z][a-z0-9_]{1,47}$")
    if pid in (reserved if reserved is not None else builtin_ids()):
        raise PluginPackageError("id 与内置插件或保留名冲突: %s" % pid)

    entry = raw.get("entry")
    if not isinstance(entry, str) or not _safe_relative_path(entry) or not entry.endswith(".py"):
        raise PluginPackageError("entry 必须是包内的 .py 相对路径")

    version = safe_text(raw.get("version"), "version", 32)
    mf = {
        "manifest_version": 1,
        "id": pid,
        "name": safe_text(raw.get("name"), "name", 80),
        "version": version,
        "description": safe_text(raw.get("description", ""), "description", 300),
        "author": safe_text(raw.get("author", ""), "author", 80),
        "icon": _safe_class(raw.get("icon", "fa-puzzle-piece"), "icon"),
        "icon_color": _safe_class(raw.get("icon_color", "from-violet-500 to-purple-600"), "icon_color"),
        "entry": entry,
        "min_app_version": safe_text(raw.get("min_app_version", ""), "min_app_version", 32),
        "permissions": _safe_permissions(raw.get("permissions", [])),
        "source": _safe_source(raw.get("source")),
    }
    return mf


def _safe_class(value, field: str) -> str:
    if not isinstance(value, str) or not SAFE_CLASS_RE.match(value):
        raise PluginPackageError("%s 只允许字母/数字/空格/下划线/连字符" % field)
    return value


def _safe_permissions(value) -> list:
    if not isinstance(value, list) or len(value) > 32:
        raise PluginPackageError("permissions 必须是数组且不超过 32 项")
    return [safe_text(p, "permissions[]", 64) for p in value]


def _safe_source(value) -> dict:
    if value is None:
        return {"type": "local"}
    if not isinstance(value, dict):
        raise PluginPackageError("source 必须是对象")
    out = {"type": safe_text(value.get("type", "local"), "source.type", 32)}
    for key in ("repo", "ref", "asset", "path"):
        if key in value:
            out[key] = safe_text(value[key], "source." + key, 160)
    return out


def _safe_relative_path(name: str) -> bool:
    """拒绝绝对路径、.. 穿越、反斜杠、盘符、NUL、URL 协议。"""
    if not isinstance(name, str) or not name or len(name) > 200:
        return False
    if "\x00" in name or "\\" in name or name.startswith("/") or name.startswith("//"):
        return False
    if re.match(r"^[A-Za-z]:", name) or "://" in name:
        return False
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return True


def validate_zip(data: bytes) -> dict:
    """严格校验并返回 ``{相对路径: 内容}``，不落盘。

    全部内容在内存中且总量受 MAX_UNCOMPRESSED_BYTES 限制，避免解压到磁盘后才发现问题。
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise PluginPackageError("插件包为空")
    if len(data) > MAX_ZIP_BYTES:
        raise PluginPackageError("插件包超过 %d 字节上限" % MAX_ZIP_BYTES)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:
        raise PluginPackageError("不是有效的 zip 包") from exc

    files = {}
    total = 0
    try:
        infos = zf.infolist()
        if not infos or len(infos) > MAX_FILES:
            raise PluginPackageError("插件包文件数超出 1~%d 的范围" % MAX_FILES)
        for info in infos:
            name = info.filename
            if not _safe_relative_path(name.rstrip("/")):
                raise PluginPackageError("非法文件路径: %s" % name[:80])
            # 目录项只允许显式以 / 结尾，且不允许携带内容
            is_dir = name.endswith("/")
            if is_dir:
                if info.file_size:
                    raise PluginPackageError("目录项不允许携带内容")
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                raise PluginPackageError("不允许的文件类型: %s" % ext)
            # 符号链接 / 设备文件一律拒绝（external_attr 高 16 位为 unix mode）
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise PluginPackageError("不允许符号链接")
            if info.file_size > MAX_SINGLE_FILE_BYTES:
                raise PluginPackageError("单文件超过 %d 字节上限" % MAX_SINGLE_FILE_BYTES)
            if info.compress_size and info.file_size // max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
                raise PluginPackageError("压缩比异常，疑似 zip bomb")
            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise PluginPackageError("解压后总大小超过 %d 字节上限" % MAX_UNCOMPRESSED_BYTES)
            content = zf.read(info)
            # read 之后复核真实长度，防止 file_size 字段伪造
            if len(content) > MAX_SINGLE_FILE_BYTES:
                raise PluginPackageError("单文件超过 %d 字节上限" % MAX_SINGLE_FILE_BYTES)
            files[name] = content
    finally:
        zf.close()
    if "manifest.json" not in files:
        raise PluginPackageError("缺少 manifest.json")
    return files


def install_from_zip(data: bytes, origin: dict | None = None, overwrite: bool = False) -> dict:
    """校验并原子安装插件包。返回规范化 manifest；**不启用、不执行任何插件代码**。"""
    files = validate_zip(data)
    try:
        raw = json.loads(files["manifest.json"].decode("utf-8"))
    except Exception as exc:
        raise PluginPackageError("manifest.json 不是有效 UTF-8 JSON") from exc
    mf = validate_manifest(raw)
    if mf["entry"] not in files:
        raise PluginPackageError("entry 指向的文件不在包内: %s" % mf["entry"])

    root = external_dir()
    os.makedirs(root, exist_ok=True)
    target = os.path.join(root, mf["id"])
    if os.path.exists(target) and not overwrite:
        raise PluginPackageError("插件 %s 已安装，如需更新请显式覆盖" % mf["id"])

    tmp = os.path.join(root, ".tmp-%s" % uuid.uuid4().hex)
    backup = None
    try:
        os.makedirs(tmp)
        for name, content in files.items():
            dest = os.path.join(tmp, *name.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fp:
                fp.write(content)
        record = {
            "source": (origin or {}).get("type", "local"),
            "repo": (origin or {}).get("repo", ""),
            "ref": (origin or {}).get("ref", ""),
            "asset": (origin or {}).get("asset", ""),
            "sha256": sha256_bytes(data),
            "imported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(os.path.join(tmp, "origin.json"), "w", encoding="utf-8") as fp:
            json.dump(record, fp, ensure_ascii=False, indent=2)
        if os.path.exists(target):
            backup = target + ".bak-" + uuid.uuid4().hex
            os.rename(target, backup)
        os.rename(tmp, target)
        tmp = None
        if backup:
            shutil.rmtree(backup, ignore_errors=True)
            backup = None
    except Exception:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        if backup and os.path.isdir(backup) and not os.path.isdir(target):
            os.rename(backup, target)
        raise
    return {"manifest": mf, "origin": record}


def list_installed() -> dict:
    """返回 ``{id: manifest}``；损坏的目录跳过，不影响其他插件。"""
    root = external_dir()
    out = {}
    if not os.path.isdir(root):
        return out
    for name in os.listdir(root):
        if name.startswith(".") or not ID_RE.match(name):
            continue
        path = os.path.join(root, name, "manifest.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fp:
                mf = validate_manifest(json.load(fp))
            if mf["id"] == name:
                out[name] = mf
        except Exception:
            continue
    return out


def get_origin(pid: str) -> dict:
    path = os.path.join(external_dir(), pid, "origin.json")
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_manifest(pid: str) -> dict:
    if not ID_RE.match(pid):
        raise PluginPackageError("非法插件 id")
    path = os.path.join(external_dir(), pid, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return validate_manifest(json.load(fp))
    except FileNotFoundError as exc:
        raise PluginPackageError("插件未安装: %s" % pid) from exc


def uninstall(pid: str) -> bool:
    """仅卸载外部插件；内置插件目录不受影响。"""
    if not ID_RE.match(pid) or pid in builtin_ids():
        raise PluginPackageError("只能卸载外部导入的插件")
    root = external_dir()
    target = os.path.realpath(os.path.join(root, pid))
    if os.path.commonpath([os.path.realpath(root), target]) != os.path.realpath(root):
        raise PluginPackageError("路径越界")
    if not os.path.isdir(target):
        return False
    shutil.rmtree(target)
    for key in [k for k in sys.modules if k == "app.plugins._external.%s" % pid
                or k.startswith("app.plugins._external.%s." % pid)]:
        sys.modules.pop(key, None)
    return True


def load_external(pid: str):
    """显式启用时按需 import 外部插件，返回 PluginBase 实例。

    导入阶段（install_from_zip）绝不调用本函数，这是「导入不执行代码」的关键。
    """
    from app.plugins.base import PluginBase  # 延迟导入：保持本模块可被纯标准库测试独立加载

    mf = get_manifest(pid)
    root = os.path.realpath(os.path.join(external_dir(), pid))
    entry = os.path.realpath(os.path.join(root, *mf["entry"].split("/")))
    if os.path.commonpath([root, entry]) != root or not os.path.isfile(entry):
        raise PluginPackageError("entry 路径非法")
    module_name = "app.plugins._external.%s" % pid
    import importlib.util

    spec = importlib.util.spec_from_file_location(module_name, entry)
    if spec is None or spec.loader is None:
        raise PluginPackageError("无法加载插件模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise

    instance = getattr(module, "plugin", None)
    if not isinstance(instance, PluginBase):
        instance = None
        for attr in dir(module):
            candidate = getattr(module, attr)
            if (isinstance(candidate, type) and issubclass(candidate, PluginBase)
                    and candidate is not PluginBase):
                instance = candidate()
                break
    if not isinstance(instance, PluginBase):
        sys.modules.pop(module_name, None)
        raise PluginPackageError("插件未提供 PluginBase 实例")
    if instance.id != pid:
        sys.modules.pop(module_name, None)
        raise PluginPackageError("插件实例 id 与 manifest 不一致")
    return instance


def manifest_meta(mf: dict, enabled: bool = False) -> dict:
    """把未加载的外部插件 manifest 转成与 PluginBase.get_meta() 同构的字典。"""
    origin = get_origin(mf["id"])
    return {
        "id": mf["id"],
        "name": mf["name"],
        "description": mf["description"],
        "icon": mf["icon"],
        "icon_color": mf["icon_color"],
        "version": mf["version"],
        "author": mf["author"],
        "enabled": enabled,
        "page_url": "/plugins/%s" % mf["id"],
        "config_schema": [],
        "pro_only": False,
        "external": True,
        "installed": True,
        "loaded": enabled,
        "source": origin.get("source", "local"),
        "origin": origin,
    }
