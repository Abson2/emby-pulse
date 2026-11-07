# 插件开发 Quickstart（5 分钟上手）

> 适用版本：v2.5 及以上（插件中心含插件市场 / 定时执行 / AI 开发提示词 / 开发指南 / ZIP 开发示例 / 单文件 .py 示例 / 刷新 / 导入插件 + OpenAPI）。
> 完整契约见 [插件开发规范](PLUGIN_DEVELOPMENT.md)；想让 AI 代写插件见 [给 AI 的话术指引](PLUGIN_AI_PROMPT.md)。

## 0. 前置与安全边界

- **导入 ≠ 执行代码，默认禁用，仅管理员启用。** 导入只把包落到插件目录并记录来源；只有你在卡片上打开开关，才会 `import` 插件代码。
- 未启用的外部插件代码**从不 import**，因此「定时执行」调度器也只会对已启用插件生效 —— 这条边界在 2.3 确立、2.5 完整保留。

## 1. 最快路径：改单文件示例

1. 插件中心 → **单文件 .py 示例** → 下载 `hello_single.py`（已含 `_setup_routes`、`on_schedule`、模块级 `plugin = HelloPlugin()`）。
2. 改 `class` 里的内容（配置 schema、只读 API、日志）。
3. 把入口文件命名为 `plugin.py`，与 `manifest.json` 一起**压成 zip**（文件在压缩包根目录，不要再套一层文件夹）。
4. 插件中心 → **导入插件** → 本地 ZIP 包上传。
5. 导入成功后默认禁用；确认代码无误再到卡片打开开关。启用后即可用管理员令牌访问其 API。

## 2. 用 ZIP 示例

- 插件中心 → **ZIP 开发示例** → 下载 `hello_plugin-1.0.0.zip`（已含 `manifest.json` + `plugin.py` + `README.md`，通过 2.3 全部安全校验）。
- 解压后改 `plugin.py` 与 `manifest.json`，重新压成 zip 导入。注意 `manifest.json` 里 **`entry` 必填**且必须指向包内某个 `.py`。

## 3. 让 AI 帮你写

- 插件中心 → **AI 开发提示词** → 复制全文给任意 AI，按其「自检清单」产出插件包，再走第 1/2 步导入。

## 4. 定时执行

- 在插件类里实现 `on_schedule(self)`（默认无动作）。
- 插件中心 → **定时执行** → 设间隔（分钟，0 = 关，上限 7 天）并保存；也可「立即执行」手动触发一次。
- 间隔存于插件自身配置 `schedule_minutes`，不新增表、不改对外接口。

## 5. 调用全局功能（OpenAPI）

- 插件中心 → 打开 **OpenAPI** 面板 → 创建访问令牌（默认 1 年；**令牌仅展示一次，请立即保存**；可随时撤销）。
- 下载 OpenAPI 规范（`/openapi.json`，文档页 `/docs`、`/redoc`），用 `Authorization: Bearer <令牌>` 调用任意受保护接口。
- 脚本与插件都能借此调用网页功能，无需持有会话 Cookie。

## 6. 发布到官方市场（可选）

- 把打好的 zip 作为 Release 产物，提 PR 更新仓库 `plugins/index.json`（条目含 `download` 相对路径与 `sha256`）。
- 服务端按「仓库 + ref + 相对路径」拼接下载地址，强制 **sha256 一致**才安装；白名单外的第三方仓库一律拒绝。

## 安全红线（打包必看）

- `manifest.json` 必填：`manifest_version=1`、`id`（正则 `^[a-z][a-z0-9_]{1,47}$`）、`name`、`version`、`entry`（包内 `.py`）。
- 拒绝：路径穿越 / 绝对路径 / 符号链接 / 非白名单扩展名 / zip bomb；元数据文本拒绝 HTML 特殊字符（防插件中心模板注入）。
- 导入只落盘 + 记录来源（local / github + sha256），绝不执行外部代码；启用才 import。
