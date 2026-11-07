#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部插件打包工具（2.3）：生成合规 zip 包并输出 sha256。

用法::

    python tools/pack_plugin.py examples/hello_plugin -o plugins/dist
    python tools/pack_plugin.py                      # 自检模式

无参数运行会被 ``tools/run_all_checks.py`` 自动发现，因此自检模式必须是
纯标准库、零副作用、成功时 rc=0；它同时验证示例插件能被服务端的
``external.validate_manifest`` / ``external.validate_zip`` 原样接受。

安全约定：本工具直接复用服务端的校验常量与函数（通过 runpy 加载，避免
触发 app 包的 FastAPI 依赖），打包结果与线上导入走同一套规则。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import runpy
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTERNAL_PY = os.path.join(ROOT, "app", "plugins", "external.py")
SAMPLE_DIR = os.path.join(ROOT, "examples", "hello_plugin")
# 固定时间戳 + 固定顺序，保证同一份源码重复打包得到逐字节相同的产物
FIXED_TIME = (2020, 1, 1, 0, 0, 0)


def _external():
    return runpy.run_path(EXTERNAL_PY)


def read_manifest(src_dir):
    path = os.path.join(src_dir, "manifest.json")
    with open(path, "r", encoding="utf-8") as fp:
        raw = json.load(fp)
    return _external()["validate_manifest"](raw), raw


def collect(src_dir):
    """按服务端 validate_zip 的约束收集文件，返回 ``[(相对路径, bytes)]``。"""
    ext = _external()
    allowed = ext["ALLOWED_EXTENSIONS"]
    out = []
    for base, dirs, files in os.walk(src_dir):
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d != "__pycache__"
        )
        for name in sorted(files):
            if name.startswith(".") or name.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(base, name)
            rel = os.path.relpath(full, src_dir).replace(os.sep, "/")
            if os.path.islink(full):
                raise ValueError("不允许符号链接: %s" % rel)
            if os.path.splitext(rel)[1].lower() not in allowed:
                raise ValueError("不允许的文件类型: %s" % rel)
            with open(full, "rb") as fp:
                data = fp.read()
            if len(data) > ext["MAX_SINGLE_FILE_BYTES"]:
                raise ValueError("单文件超过上限: %s" % rel)
            out.append((rel, data))
    if not out:
        raise ValueError("插件目录为空")
    if len(out) > ext["MAX_FILES"]:
        raise ValueError("文件数超过 %d" % ext["MAX_FILES"])
    if sum(len(d) for _, d in out) > ext["MAX_UNCOMPRESSED_BYTES"]:
        raise ValueError("解压后总大小超过上限")
    return out


def build_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, data in files:
            info = zipfile.ZipInfo(rel, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix：external_attr 按 unix mode 解释
            info.external_attr = 0o644 << 16  # 普通文件，绝不是符号链接
            zf.writestr(info, data)
    return buf.getvalue()


def build_from_dir(src_dir):
    mf, _ = read_manifest(src_dir)
    files = collect(src_dir)
    names = {rel for rel, _ in files}
    if mf["entry"] not in names:
        raise ValueError("entry 指向的文件不在包内: %s" % mf["entry"])
    data = build_zip(files)
    # 最终自检：走线上导入完全相同的校验链，任何不合规都在本机先暴露
    _external()["validate_zip"](data)
    return mf, data


def self_check():
    passed = [0]
    failed = []

    def check(cond, msg):
        if cond:
            passed[0] += 1
            print("  PASS  " + msg)
        else:
            failed.append(msg)
            print("  FAIL  " + msg)

    print("== pack_plugin 自检（无参数模式，供全量校验网自动发现）==")
    ext = _external()
    check(os.path.isfile(EXTERNAL_PY), "服务端校验模块存在")
    check(os.path.isdir(SAMPLE_DIR), "示例插件目录存在")

    mf, raw = read_manifest(SAMPLE_DIR)
    check(mf["id"] == "hello_plugin" and mf["manifest_version"] == 1,
          "示例 manifest 通过服务端 validate_manifest")
    check(mf["min_app_version"] == "2.3", "示例声明最低应用版本 2.3")

    files = collect(SAMPLE_DIR)
    names = {rel for rel, _ in files}
    check("manifest.json" in names, "包内含 manifest.json")
    check(mf["entry"] in names, "entry 指向的文件在包内")
    check(all(os.path.splitext(n)[1].lower() in ext["ALLOWED_EXTENSIONS"]
              for n in names), "所有文件扩展名在白名单内")

    data = build_zip(files)
    check(build_zip(files) == data, "固定时间戳：重复打包逐字节可复现")
    check(len(data) <= ext["MAX_ZIP_BYTES"], "产物大小在上限内")
    parsed = ext["validate_zip"](data)
    check(set(parsed) == names, "产物通过服务端 validate_zip 且文件集合一致")
    check(json.loads(parsed["manifest.json"].decode("utf-8"))["id"] == mf["id"],
          "包内 manifest 与目录内 manifest 一致")

    # 负面用例：危险扩展名必须被收集阶段拒绝
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fp:
            json.dump(raw, fp)
        with open(os.path.join(tmp, "payload.exe"), "wb") as fp:
            fp.write(b"MZ")
        try:
            collect(tmp)
            rejected = False
        except ValueError:
            rejected = True
        check(rejected, "危险扩展名在打包阶段被拒绝")

    print("\npassed: %d  failed: %d" % (passed[0], len(failed)))
    if failed:
        for msg in failed:
            print("  - " + msg)
        return 1
    print("PACK PLUGIN SELF CHECK PASSED")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # run_all_checks.py 会以 "." 作为唯一参数调用本工具；此时应进入自检模式，
    # 而不是把仓库根目录当成插件目录去打包。
    if not argv or os.path.abspath(argv[0]) == ROOT:
        return self_check()

    ap = argparse.ArgumentParser(description="打包 EmbyPulse 2.3 外部插件")
    ap.add_argument("plugin_dir", help="插件源码目录（含 manifest.json）")
    ap.add_argument("-o", "--out", default=".", help="产物输出目录")
    args = ap.parse_args(argv)

    src_dir = os.path.abspath(args.plugin_dir)
    if not os.path.isdir(src_dir):
        print("ERROR: 目录不存在: %s" % src_dir, file=sys.stderr)
        return 2
    try:
        mf, data = build_from_dir(src_dir)
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    filename = "%s-%s.zip" % (mf["id"], mf["version"])
    path = os.path.join(out_dir, filename)
    with open(path, "wb") as fp:
        fp.write(data)
    digest = hashlib.sha256(data).hexdigest()

    print("插件包: %s" % path)
    print("大小:   %d bytes" % len(data))
    print("sha256: %s" % digest)
    print("\n把下面条目追加到 plugins/index.json 的 plugins 数组：")
    print(json.dumps({
        "id": mf["id"],
        "name": mf["name"],
        "version": mf["version"],
        "description": mf["description"],
        "download": "plugins/dist/%s" % filename,
        "sha256": digest,
        "min_app_version": mf["min_app_version"] or "2.3",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
