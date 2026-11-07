"""2.3 插件导入 / GitHub 发布窗口 / OpenAPI 的纯标准库回归。

CI verify 只提供标准库，因此这里用 runpy.run_path 直接加载两个纯标准库模块，
不触发 app 包的 FastAPI 依赖；OpenAPI 部分用 AST / 源码形态契约锁定。
"""
import ast
import io
import json
import os
import runpy
import stat
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = runpy.run_path(os.path.join(ROOT, "app", "plugins", "external.py"))
GH = runpy.run_path(os.path.join(ROOT, "app", "plugins", "github_source.py"))

PASS = 0
FAILS = []


def check(cond, msg):
    global PASS
    if cond:
        PASS += 1
        print("  PASS  " + msg)
    else:
        FAILS.append(msg)
        print("  FAIL  " + msg)


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fp:
        return fp.read()


def make_zip(entries, manifest=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if manifest is not None:
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for name, payload in entries.items():
            if isinstance(payload, dict):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = payload.get("mode", 0o644) << 16
                zf.writestr(info, payload.get("data", b""))
            else:
                zf.writestr(name, payload)
    return buf.getvalue()


def base_manifest(**overrides):
    mf = {
        "manifest_version": 1,
        "id": "demo_plugin",
        "name": "演示插件",
        "version": "1.0.0",
        "description": "用于回归的插件",
        "author": "tester",
        "entry": "plugin.py",
        "min_app_version": "2.3",
        "permissions": [],
    }
    mf.update(overrides)
    return mf


print("== 1. zip 包校验与安装（导入只落盘、默认禁用） ==")
_tmpdir = tempfile.TemporaryDirectory()
os.environ["PLUGIN_EXTERNAL_DIR"] = _tmpdir.name

good = make_zip({"plugin.py": b"# demo\n"}, manifest=base_manifest())
try:
    result = EXT["install_from_zip"](good, {"type": "local"})
    mf = result["manifest"]
    check(mf["id"] == "demo_plugin" and mf["entry"] == "plugin.py", "合法包安装成功并返回规范化 manifest")
    check(os.path.isfile(os.path.join(_tmpdir.name, "demo_plugin", "plugin.py")), "插件文件落在外部持久化目录")
    check(os.path.isfile(os.path.join(_tmpdir.name, "demo_plugin", "origin.json")), "写入 origin.json 来源记录")
    origin = EXT["get_origin"]("demo_plugin")
    check(origin.get("source") == "local" and len(origin.get("sha256", "")) == 64, "origin 记录来源与 sha256")
    check("enabled" not in origin and not os.path.exists(os.path.join(_tmpdir.name, "demo_plugin", "enabled.flag")),
          "安装阶段不写任何启用标记（默认禁用）")
    check("demo_plugin" in EXT["list_installed"](), "list_installed 能列出已安装外部插件")
    check(EXT["list_installed"]()["demo_plugin"]["id"] == "demo_plugin", "list_installed 返回规范化 manifest")
except Exception as exc:
    check(False, "合法包安装不应抛异常: %r" % exc)
    mf = {}

check("manifest_meta" in EXT, "提供未加载插件的 manifest 元信息转换")
if mf:
    meta = EXT["manifest_meta"](mf)
    check(meta["enabled"] is False and meta["external"] is True and meta["loaded"] is False,
          "未启用外部插件的 meta 为 enabled=False / external=True / loaded=False")
    check(meta["source"] == "local", "meta 携带来源")

print("\n== 2. 重复安装 / 覆盖 ==")
try:
    EXT["install_from_zip"](good, {"type": "local"})
    check(False, "重复安装默认应被拒绝")
except EXT["PluginPackageError"]:
    check(True, "重复安装默认被拒绝")
try:
    EXT["install_from_zip"](good, {"type": "local"}, True)
    check(True, "显式 overwrite=True 允许覆盖")
except Exception as exc:
    check(False, "显式覆盖不应抛异常: %r" % exc)

print("\n== 3. 拒绝危险包 ==")
bad_cases = [
    ("路径穿越", make_zip({"../../evil.py": b"x"}, manifest=base_manifest())),
    ("绝对路径", make_zip({"/tmp/evil.py": b"x"}, manifest=base_manifest())),
    ("反斜杠路径", make_zip({"a\\evil.py": b"x"}, manifest=base_manifest())),
    ("危险扩展名", make_zip({"evil.exe": b"x"}, manifest=base_manifest())),
    ("符号链接", make_zip({"link.py": {"mode": stat.S_IFLNK | 0o777, "data": b"plugin.py"}},
                      manifest=base_manifest())),
    ("缺少 manifest", make_zip({"plugin.py": b"x"}, manifest=None)),
    ("非法 id（大写/穿越）", make_zip({"plugin.py": b"x"}, manifest=base_manifest(id="../Evil"))),
    ("manifest_version 不为 1", make_zip({"plugin.py": b"x"}, manifest=base_manifest(manifest_version=2))),
    ("名称含 HTML 注入", make_zip({"plugin.py": b"x"}, manifest=base_manifest(name="<script>x</script>"))),
    ("内置插件 id 冲突", make_zip({"plugin.py": b"x"}, manifest=base_manifest(id="hdhive"))),
    ("保留名冲突", make_zip({"plugin.py": b"x"}, manifest=base_manifest(id="external"))),
    ("entry 非 .py", make_zip({"plugin.txt": b"x"}, manifest=base_manifest(entry="plugin.txt"))),
    ("空包", b""),
    ("非 zip 数据", b"not-a-zip-at-all"),
]
for label, payload in bad_cases:
    try:
        EXT["validate_zip"](payload)
        # 对 manifest 类错误，validate_zip 可能通过但 validate_manifest 必须拒绝
        try:
            files = EXT["validate_zip"](payload)
            raw = json.loads(files["manifest.json"].decode("utf-8"))
            EXT["validate_manifest"](raw)
            rejected = False
        except Exception:
            rejected = True
        check(rejected, "拒绝：%s" % label)
    except Exception:
        check(True, "拒绝：%s" % label)

try:
    EXT["install_from_zip"](
        make_zip({"other.py": b"x"}, manifest=base_manifest(entry="plugin.py")),
        {"type": "local"}, True,
    )
    check(False, "entry 指向包外文件应被拒绝")
except EXT["PluginPackageError"]:
    check(True, "拒绝：entry 不在包内")

huge = make_zip({"plugin.py": b"x"}, manifest=base_manifest())
try:
    EXT["validate_zip"](huge + b"0" * (EXT["MAX_ZIP_BYTES"] + 1))
    check(False, "超过包大小上限应被拒绝")
except EXT["PluginPackageError"]:
    check(True, "拒绝：超过包大小上限")

print("\n== 4. 卸载边界 ==")
try:
    EXT["uninstall"]("hdhive")
    check(False, "内置插件不得卸载")
except EXT["PluginPackageError"]:
    check(True, "内置插件不得卸载")
check(EXT["uninstall"]("demo_plugin") is True, "外部插件可卸载")
check("demo_plugin" not in EXT["list_installed"](), "卸载后不再出现在已安装列表")
check(EXT["uninstall"]("demo_plugin") is False, "重复卸载返回 False")

print("\n== 5. GitHub 发布窗口：URL 拼接与白名单 ==")
check(GH["registry_url"]("Abson2/emby-pulse", "main")
      == "https://raw.githubusercontent.com/Abson2/emby-pulse/main/plugins/index.json",
      "索引 URL 由服务端固定拼接")
check(GH["asset_url"]("Abson2/emby-pulse", "v2.3", "plugins/dist/demo.zip")
      == "https://raw.githubusercontent.com/Abson2/emby-pulse/v2.3/plugins/dist/demo.zip",
      "产物 URL 由 repo/ref/相对路径拼接")


def rejects(fn, *args):
    try:
        fn(*args)
        return False
    except Exception:
        return True


for label, url in [
    ("http 明文", "http://raw.githubusercontent.com/Abson2/emby-pulse/main/plugins/index.json"),
    ("非白名单域名", "https://evil.example.com/a.zip"),
    ("localhost 内网", "https://127.0.0.1/a.zip"),
    ("凭据注入", "https://user:pw@raw.githubusercontent.com/a.zip"),
    ("非 443 端口", "https://raw.githubusercontent.com:8443/a.zip"),
]:
    check(rejects(GH["assert_safe_url"], url), "拒绝 URL：%s" % label)
check(GH["assert_safe_url"]("https://raw.githubusercontent.com/a/b.zip") is None, "白名单 HTTPS URL 放行")

check(rejects(GH["validate_repo"], "evil/repo"), "仓库白名单拒绝第三方仓库")
check(GH["validate_repo"]("Abson2/emby-pulse") == "Abson2/emby-pulse", "本项目仓库在白名单内")
check(rejects(GH["current_ref"], "main/../../evil"), "ref 拒绝路径穿越")
check(rejects(GH["current_ref"], "main evil"), "ref 拒绝空格")
check(GH["current_ref"]("v2.3") == "v2.3", "合法 tag ref 放行")
check(rejects(GH["validate_asset_path"], "../../secret.zip"), "download 拒绝 ..")
check(rejects(GH["validate_asset_path"], "https://evil.com/a.zip"), "download 拒绝绝对 URL")
check(rejects(GH["validate_asset_path"], "plugins/dist/a.tar"), "download 必须是 .zip")
check(GH["validate_asset_path"]("plugins/dist/a.zip") == "plugins/dist/a.zip", "合法 download 放行")

print("\n== 6. 索引校验与产物 sha256 ==")
valid_entry = {
    "id": "demo_plugin", "name": "演示", "version": "1.0.0",
    "download": "plugins/dist/demo_plugin-1.0.0.zip",
    "sha256": "a" * 64,
}
reg = GH["validate_registry"]({"schema": 1, "repository": "Abson2/emby-pulse", "plugins": [valid_entry]},
                              "Abson2/emby-pulse")
check(reg["plugins"][0]["id"] == "demo_plugin", "合法索引通过校验")
check(rejects(GH["validate_registry"], {"schema": 2, "repository": "Abson2/emby-pulse", "plugins": []},
              "Abson2/emby-pulse"), "拒绝 schema != 1")
check(rejects(GH["validate_registry"], {"schema": 1, "repository": "evil/repo", "plugins": []},
              "Abson2/emby-pulse"), "拒绝索引 repository 与请求仓库不一致")
check(rejects(GH["validate_registry"],
              {"schema": 1, "repository": "Abson2/emby-pulse",
               "plugins": [dict(valid_entry, sha256="nope")]},
              "Abson2/emby-pulse"), "拒绝缺少合法 sha256 的条目")
check(GH["find_entry"](reg, "demo_plugin")["id"] == "demo_plugin", "按 id 找到索引条目")
check(rejects(GH["find_entry"], reg, "missing"), "不存在的插件被拒绝")

_globals = GH["fetch_asset"].__globals__
_orig_http = _globals["http_get"]
try:
    _globals["http_get"] = lambda *a, **k: b"tampered"
    check(rejects(GH["fetch_asset"], dict(valid_entry, sha256="a" * 64), "Abson2/emby-pulse", "main"),
          "产物 sha256 不匹配时丢弃")
    digest = GH["sha256_bytes"](b"payload")
    _globals["http_get"] = lambda *a, **k: b"payload"
    check(GH["fetch_asset"](dict(valid_entry, sha256=digest), "Abson2/emby-pulse", "main") == b"payload",
          "sha256 匹配时返回产物")
finally:
    _globals["http_get"] = _orig_http

print("\n== 7. 接口与 OpenAPI 静态契约 ==")
router_src = read("app/domains/plugins/router.py")
main_src = read("app/main.py")
guard_src = read("app/core/openapi_guard.py")
mw_src = read("app/bootstrap/middleware.py")
ci_src = read(".github/workflows/docker-publish.yml")

for path in ("/api/plugins/import", "/api/plugins/import/github", "/api/plugins/registry",
             "/api/plugins/{plugin_id}/install"):
    check('"%s"' % path in router_src, "导入接口存在: %s" % path)
check("user_service.is_admin_user(request)" in router_src and "_admin_ok" in router_src,
      "导入接口逐条做管理员判定（纵深防御）")
check("set_plugin_enabled(mf[\"id\"], False)" in router_src, "导入后强制写回禁用状态")
check("delete_plugin_state(plugin_id)" in router_src, "卸载时清理启用状态")
check("run_in_threadpool" in router_src, "网络/落盘在线程池执行，不阻塞事件循环")

check('openapi_url="/openapi.json"' in main_src, "显式启用 /openapi.json")
check("configure_openapi_docs(app)" in main_src and '"/docs"' in main_src and '"/redoc"' in main_src,
      "自定义提供 /docs 与 /redoc 文档页")
check('nonce=' in main_src and "csp_nonce" in main_src, "文档页内联脚本注入 CSP nonce")
check("OPENAPI_PUBLIC" in guard_src and "is_admin_request" in guard_src,
      "OpenAPI 默认管理员鉴权且提供显式匿名开关")
check("OpenAPIGuardMiddleware" in mw_src and "DatabaseSession" in mw_src,
      "OpenAPI 守卫注册在会话中间件内层")
check("is_admin_request" in read("app/core/auth_gate_middleware.py"),
      "管理员判定与 AuthGate 共用同一函数")
check("/openapi.json" in ci_src and "OpenAPI" in ci_src, "CI 容器冒烟验证 OpenAPI 匿名不可读")

print("\n== 8. 加载器安全边界 ==")
init_src = read("app/plugins/__init__.py")
check("discover_external_plugins()" in init_src, "启动时登记外部插件")
check("enabled = {row[\"plugin_id\"] for row in plugin_dao.list_plugin_states() if row[\"enabled\"]}"
      in init_src, "仅对数据库已启用的外部插件执行 import")
check("external.load_external(plugin_id)" in init_src, "首次显式启用时按需加载")
check("uninstall_plugin" in init_src, "提供卸载入口")
check("from app.plugins import external" in init_src, "加载器接入外部包管理")

print("\n== 9. 两个新模块只依赖标准库 ==")
_stdlib = {
    "__future__", "hashlib", "io", "json", "os", "re", "shutil", "stat", "sys", "uuid",
    "zipfile", "datetime", "urllib", "urllib.error", "urllib.parse", "urllib.request",
    "importlib", "importlib.util", "typing",
}
for rel in ("app/plugins/external.py", "app/plugins/github_source.py"):
    tree = ast.parse(read(rel))
    imports = set()
    # 只检查模块顶层 import：函数内的延迟导入不影响纯标准库加载
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
    outside = sorted(i for i in imports if i.split(".")[0] not in _stdlib and i != "__future__")
    check(not outside, "%s 顶层只依赖标准库（越界: %s）" % (rel, outside))

print("\n== 10. 官方发布窗口端到端（离线） ==")
reg_raw = json.loads(read("plugins/index.json"))
try:
    registry_data = GH["validate_registry"](reg_raw, "Abson2/emby-pulse")
    check(True, "官方索引通过服务端 validate_registry")
except Exception as exc:
    registry_data = {"plugins": []}
    check(False, "官方索引通过服务端 validate_registry: %s" % exc)
check(len(registry_data.get("plugins", [])) >= 1, "索引至少发布一个官方插件")
if registry_data.get("plugins"):
    gh_entry = GH["find_entry"](registry_data, registry_data["plugins"][0]["id"])
    gh_url = GH["asset_url"]("Abson2/emby-pulse", "main", gh_entry["download"])
    GH["assert_safe_url"](gh_url)
    check(gh_url.startswith(
        "https://raw.githubusercontent.com/Abson2/emby-pulse/main/plugins/dist/"),
        "下载地址由服务端按仓库+ref+相对路径拼接")
    asset_rel = os.path.join("plugins", "dist", os.path.basename(gh_entry["download"]))
    asset_path = os.path.join(ROOT, asset_rel)
    check(os.path.isfile(asset_path), "官方产物已随仓库发布: %s" % asset_rel)
    if os.path.isfile(asset_path):
        with open(asset_path, "rb") as fp:
            asset_bytes = fp.read()
        check(GH["sha256_bytes"](asset_bytes) == gh_entry["sha256"],
              "产物 sha256 与索引逐字一致")
        try:
            ep_files = EXT["validate_zip"](asset_bytes)
            ep_mf = EXT["validate_manifest"](
                json.loads(ep_files["manifest.json"].decode("utf-8")))
            check(ep_mf["id"] == gh_entry["id"] and ep_mf["entry"] in ep_files,
                  "官方产物通过线上同一套包校验且 manifest 一致")
        except Exception as exc:
            check(False, "官方产物通过线上同一套包校验: %s" % exc)
        old_env = os.environ.get("PLUGIN_EXTERNAL_DIR")
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["PLUGIN_EXTERNAL_DIR"] = tmpdir
            try:
                modules_before = set(sys.modules)
                installed = EXT["install_from_zip"](
                    asset_bytes,
                    {"type": "github", "repo": "Abson2/emby-pulse",
                     "ref": "main", "asset": gh_entry["download"]},
                )
                check(installed["origin"]["source"] == "github"
                      and installed["origin"]["sha256"] == gh_entry["sha256"],
                      "安装写入可审计的 GitHub 来源")
                check(not any("app.plugins._external" in m
                              for m in set(sys.modules) - modules_before),
                      "导入阶段未 import 任何外部插件代码")
                check(os.path.isfile(os.path.join(tmpdir, gh_entry["id"], "manifest.json")),
                      "插件落在持久化外部目录")
                check(EXT["list_installed"]().get(gh_entry["id"], {}).get("id") == gh_entry["id"],
                      "已安装清单可枚举")
                check(EXT["uninstall"](gh_entry["id"]) is True, "官方插件可正常卸载")
            except Exception as exc:
                check(False, "官方产物安装/卸载: %s" % exc)
            finally:
                if old_env is None:
                    os.environ.pop("PLUGIN_EXTERNAL_DIR", None)
                else:
                    os.environ["PLUGIN_EXTERNAL_DIR"] = old_env

print("\n== 11. 插件中心前端与开发规范静态契约 ==")
ui = read("templates/plugins.html")
for token in (
    'id="plugin-import-modal"', 'id="pi-registry-list"', 'id="pi-file"',
    "openPluginImportModal()", "submitLocalPlugin()", "loadGithubRegistry()",
    "importGithubPlugin(", "uninstallExternalPlugin(", "toggleExternalPlugin(",
    "function epEsc(", "box.textContent = text",
):
    check(token in ui, "前端入口存在: %s" % token)
check("const isExternal = p.external === true;" in ui
      and "const idAttr = isExternal ?" in ui
      and "const coreButtons = (isExternal && !p.loaded) ? '' :" in ui,
      "内置插件卡片 DOM 保持原样，仅 external 才追加来源/卸载")
check("epEsc(item.name)" in ui and "epEsc(item.description)" in ui
      and 'data-gh-id="' in ui,
      "GitHub 索引远程字段转义且 id 不拼进 JS 字符串")
check(ui.count("<script") == 1, "未新增 script 标签，沿用 CSP nonce")
for rel in (
    "docs/PLUGIN_DEVELOPMENT.md", "docs/PLUGIN_AI_PROMPT.md",
    "examples/hello_plugin/manifest.json", "examples/hello_plugin/plugin.py",
    "tools/pack_plugin.py", "plugins/index.json", "plugins/README.md",
):
    check(os.path.isfile(os.path.join(ROOT, rel)), "开发资料存在: %s" % rel)
ai_doc = read("docs/PLUGIN_AI_PROMPT.md")
check("【主 Prompt】" in ai_doc and "== 我的需求 ==" in ai_doc
      and "自检清单" in ai_doc, "AI 话术指引含自包含主 Prompt 与自检清单")
dev_doc = read("docs/PLUGIN_DEVELOPMENT.md")
check("导入 ≠ 启用" in dev_doc and "PluginBase" in dev_doc
      and "manifest_version" in dev_doc, "开发规范覆盖导入边界/基类/manifest")

print("\npassed: %d  failed: %d" % (PASS, len(FAILS)))
if FAILS:
    for msg in FAILS:
        print("  - " + msg)
    sys.exit(1)
print("PLUGIN IMPORT 2.3 CHECKS PASSED")
