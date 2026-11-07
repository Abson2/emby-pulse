# 官方插件发布窗口

本目录是 **emby-pulse 主体项目的插件发布窗口**。2.3 起，插件中心的「从 GitHub 导入」只读取这里的索引与产物；未来新增插件**只需改本目录，无需改动主体代码**。

当前已发布一个官方示例 `hello_plugin`（源码见 `examples/hello_plugin/`），可直接在插件中心「GitHub 官方索引」里拉取验证整条链路；它只提供一个只读 API，默认禁用，无副作用。

## 目录约定

```
plugins/
├── index.json                      # 官方插件索引（必填，schema=1）
├── README.md                       # 本说明
└── dist/
    └── <plugin_id>-<version>.zip   # 插件包产物
```

## 发布一个新插件

1. 按 [插件开发规范](../docs/PLUGIN_DEVELOPMENT.md) 写好插件目录（至少 `manifest.json` + `plugin.py`）；也可用 [给 AI 的话术指引](../docs/PLUGIN_AI_PROMPT.md) 让 AI 直接生成。
2. 用打包脚本生成产物（会同时打印 sha256）：

   ```bash
   python tools/pack_plugin.py examples/hello_plugin -o plugins/dist
   ```

3. 在 `plugins/index.json` 的 `plugins` 数组追加条目：

   ```json
   {
     "id": "hello_plugin",
     "name": "示例插件",
     "version": "1.0.0",
     "description": "一句话说明",
     "download": "plugins/dist/hello_plugin-1.0.0.zip",
     "sha256": "<上一步输出的 64 位十六进制>",
     "min_app_version": "2.3"
   }
   ```

4. 提交并推送到默认分支（`main`）。用户在插件中心点「从 GitHub 导入」即可看到并安装。

## 服务端校验（不满足即拒绝）

| 项 | 规则 |
| --- | --- |
| 仓库 | 默认仅 `Abson2/emby-pulse`；可用 `PLUGIN_GITHUB_REPOS` 追加白名单 |
| 分支 / tag | `PLUGIN_GITHUB_REF`，默认 `main`；只允许 `[A-Za-z0-9._-]{1,64}` |
| 索引地址 | 服务端固定拼接 `https://raw.githubusercontent.com/<repo>/<ref>/plugins/index.json` |
| 产物地址 | 条目 `download` 必须是仓库内 `.zip` 相对路径，禁止 `..`、`://`、绝对路径 |
| 完整性 | 条目必须提供 `sha256`，下载后逐字节校验，不一致直接丢弃 |
| 体积 | 索引 ≤ 512 KiB，产物 ≤ 5 MiB，解压后 ≤ 10 MiB |
| 安装 | 复用本地 zip 全量校验；**导入默认禁用**，管理员显式启用后才加载代码 |

## 为什么产物放在仓库而不是 Release

服务端只按「仓库 + ref + 相对路径」拼接下载地址，调用方无法传入任意 URL，从根本上避免 SSRF。配合仓库白名单、域名白名单、逐跳重定向校验与 sha256，形成完整供应链闭环。后续如需支持 Release 资产，可在 `app/plugins/github_source.py` 增加一种受控的 asset 解析方式，其余校验链保持不变。

## 版本与回滚

索引随仓库分支走，用户可在导入时指定 `ref` 安装特定 tag。升级插件推荐用新版本号 + 新产物，保留旧条目以便回滚。

> 产物由 `tools/pack_plugin.py` 固定时间戳与文件顺序生成，同一份源码重复打包逐字节一致，`sha256` 可复现。改动示例插件源码后，务必重新打包并同步更新 `index.json` 的 `sha256`，否则服务端会拒绝安装。

## 相关文档

- [插件开发规范](../docs/PLUGIN_DEVELOPMENT.md)：包格式、`PluginBase` 契约、鉴权、配置、安全边界、排障
- [给 AI 的话术指引](../docs/PLUGIN_AI_PROMPT.md)：可整段复制的 Prompt，用一句话需求生成合规插件
- [示例插件源码](../examples/hello_plugin/)：最小可运行参考实现
- [2.3 变更说明](../CHANGELOG.md)：本版本完整更新内容
