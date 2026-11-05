# 求片完结关键词清理（TgtoDrive 插件）

运行在 **TgtoDrive 容器内**的 OpenAPI 插件，与 EMBYPULSE 侧的「求片同步 TgtoDrive 白名单」插件（插件 A）配合，形成"推送 + 清理"的闭环。

## 分工

- **插件 A（EMBYPULSE 侧）**：把工单大厅审批通过 / 手动接单的求片，按 TMDB 编号写入 TgtoDrive「频道监控 → 关键词白名单」，规则 id 带 `kw_` 前缀（如 `kw_123_0`），**只写不删**。
- **本插件（TgtoDrive 侧）**：按周期读取白名单里 `kw_` 前缀的规则，以「影视是否已入库 Emby」判定完结；完结满 `cleanup_days`（默认 5）天后，删除对应关键词，释放频道监控配额。**只删 `kw_` 本插件所有的规则，绝不碰用户其它关键词。**

> 为什么清理放在 TgtoDrive 侧：早期方案在 EMBYPULSE 侧做清理，因白名单是全量覆盖写，会误删用户手动添加的关键词。清理权交给 TgtoDrive 容器内的插件，避免跨容器误删。

## 完结判据

影视已在 Emby 媒体库中存在：

1. 优先用 `ProviderIds.Tmdb` 精确匹配（`AnyProviderIdEquals=Tmdb.{tmdb_id}`）；
2. 兜底按「名称 + 年份」匹配。

未入库则不计时、不删除；入库后才开始计时，满期才删。Emby 不可达时保守跳过（不删），避免误删仍在监控中的影视。

## 安装

1. 将本目录下的 `plugin.py` 放入 TgtoDrive 的插件目录（按 TgtoDrive 的插件命名要求放置，例如 `emby-pulse-monitor.py` 或对应插件 id 的目录）。
2. 在 TgtoDrive 插件管理中启用，并按下方说明填写参数、设置运行周期（建议每天一次或每 6 小时一次）。
3. 本插件只使用 Python 标准库，无需额外依赖。

## 参数

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `tgtdrive_base_url` | TgtoDrive 地址（插件运行在容器内，通常用回环地址） | `http://127.0.0.1:12366` |
| `tgtdrive_token` | TgtoDrive OpenAPI 令牌（Bearer） | 空 |
| `scene` | 白名单场景 key（光鸭=ENV_FILTER_GUANGYA） | `ENV_FILTER_GUANGYA` |
| `whitelist_endpoint` | 白名单写入接口 | `/api/visual-filter/config` |
| `emby_url` | Emby 地址（用于判定入库） | `http://127.0.0.1:8096` |
| `emby_key` | Emby API Key | 空 |
| `cleanup_days` | 完结后清理天数 | `5` |

> `cleanup_days` 设为 `0` 表示「永不清理」（直接跳过本轮，保留所有 kw_ 关键词）；不填（空）才回落默认 `5`。
>
> 若 TgtoDrive / Emby 不在回环地址上，请把对应地址改成实际可达地址（保证插件容器能访问到）。

## 运行逻辑（每轮）

1. 读取白名单，挑出 `kw_` 前缀规则；
2. 对每条规则查 Emby 是否已入库；
3. 已入库：首次记录入库时间；满 `cleanup_days` 天则加入删除队列；
4. 未入库：清除计时（等真正入库再计时）；
5. 删除到期规则（读-过滤-整体 POST 覆盖，丢弃陈旧 raw_regex）；
6. 把计时状态写入插件 data_dir 的 `stats.json`（更新插件后保留，卸载时删除）。

插件运行日志会打印本轮检查结果（检查数 / 已入库 / 未入库 / 删除数）。
