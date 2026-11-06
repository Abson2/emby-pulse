# 容器排查报告：管理端 Bark 新片入库通知不触发

> 适用范围：emby-pulse-hardened（本项目 Abson2/emby-pulse fork）
> 关联需求：管理端「Bark 新片入库通知」修复（调用「自定义通知模板」插件）
> 涉及版本：根因在 `v1.9.2` 修复；Bark 正文改走 `notify_template` 插件为 `v1.9.3` 增强

---

## 1. 现象（Symptom）

- 管理端「系统设置 → 通知」中已启用 Bark，并正确填写服务器地址与 Device Key；
- **新的求片**提交时，Bark 能正常推送（用户侧验证通过）；
- **新片入库**（电影入库 / 剧集新集入库）后，Bark **不推送**，且工单大厅流程流转已正常入库；
- 容器日志中**没有任何报错**，也没有「入库通知」相关告警，排查时极易误判为 Bark 配置问题。

---

## 2. 影响范围（Scope）

| 维度 | 说明 |
| --- | --- |
| 触发条件 | 仅配置了 Bark（未配置 Telegram Bot Token / 企业微信 corpid）的部署 |
| 受影响事件 | `notify.library.new_item`（电影入库）、`notify.library.new_episode`（剧集新集入库）|
| 不受影响 | 求片通知（直接调用，不走事件总线）、TG / 企业微信 入库通知（有订阅者）|
| 数据丢失 | 无，入库动作本身正常，仅通知链路静默失效 |

---

## 3. 根因分析（Root Cause）

### 3.1 订阅门控提前退出（核心根因，v1.9.2 修复）

`app/bot/notification_bot/bot_service.py` 的 `NotificationBot.start()` 原逻辑：

```python
def start(self):
    if self.running:
        return
    # 旧逻辑：未配置 TG 且未配置企微 → 直接退出，连事件订阅都不建立
    if not get_bot_tg_token() and not get_wecom_corpid():
        return
    self._subscribe_events()   # ← 这两行在 return 之后，根本不会执行
    ...
```

系统级 Bark 入库处理器挂在 `notify.library.new_item` / `notify.library.new_episode`，
而这两个事件的**唯一订阅者是 `NotificationBot`**。当只启用 Bark 时，上面的提前
`return` 导致事件订阅从未建立。

### 3.2 事件总线静默丢弃（掩盖根因）

`app/core/event_bus.py` 的 `publish()` 对「无订阅者」的情况是**静默空转**：不抛异常、
不记日志、不告警。因此现场看不到任何报错，管理员无从判断是「没订阅」还是「Bark 挂了」。

### 3.3 求片 Bark 为何正常

求片通知由 `submit_router.py` 在用户提交时**直接调用** `safe_push("request_new", ...)`
（点对点调用，不经过事件总线），因此不受上述订阅门控影响——这正是「求片能推、入库不推」的
关键区别。

---

## 4. 容器内排查步骤（Diagnosis）

进入运行中的容器（假设服务名为 `emby-pulse` / 镜像 `emby-pulse-hardened`）：

```bash
# 进入容器
docker exec -it emby-pulse /bin/sh

# (1) 查看启动日志，确认事件订阅是否建立
#     期望看到（v1.9.2+）：
#     🤖 Notification Bot Started (event subscriptions active, tg=False, wecom=False)
#     若只配置了 Bark，tg/wecom 均为 False 但仍应 "event subscriptions active"
docker logs emby-pulse 2>&1 | grep -i "notification bot started"

# (2) 若日志显示完全缺失上述行 → 说明 Bot 未启动或仍在提前 return，升级到 v1.9.2+
```

```bash
# (3) 在容器内确认 Bark 运行期配置是否就绪（enabled / device_key / server）
python - <<'PY'
from app.domains.notifications.bark_push import _runtime_config
cfg = _runtime_config()
print("enabled      :", cfg.get("enabled"))
print("device_key?  :", bool(str(cfg.get("device_key") or "").strip()))
print("server       :", cfg.get("server"))
print("notify_library_new:", cfg.get("notify_library_new"))
PY

# (4) 手动发一条测试推送，验证服务器与 Device Key 是否可用
python - <<'PY'
from app.domains.notifications.bark_push import send_test
print(send_test())
PY
#     返回 ok=True 说明 Bark 链路本身健康
```

```bash
# (5) 确认入库事件订阅（仅 v1.9.2+ 才有意义）
python - <<'PY'
from app.core.event_bus import bus
subs = getattr(bus, "handlers", {})
for evt in ("notify.library.new_item", "notify.library.new_episode"):
    print(evt, "->", len(subs.get(evt, [])), "subscriber(s)")
PY
#     期望两个事件各有 >=1 个订阅者；若为 0 → 订阅未建立（回到步骤 1）
```

```bash
# (6) 确认「入库通知总开关」与 Bark 各自开关（二者独立）
#     入库通知总开关 get_enable_library_notify()：控制 TG/企微 图文体
#     Bark 开关 notify_library_new：控制 Bark 入库推送（独立，不受总开关影响）
```

### 快速定位决策树

```
Bark 测试推送 ok?
 ├─ 否 → Bark 服务器/Key 配置问题（步骤 3/4）
 └─ 是 → 入库事件有订阅者?
          ├─ 否 → 版本 < v1.9.2，需升级（根因 3.1）
          └─ 是 → 检查 notify_library_new 开关是否开启（根因 3.3 之外的独立开关）
```

---

## 5. 修复方案（Fix）

### 5.1 v1.9.2 — 订阅门控修复（根治）

`NotificationBot.start()` 改为：**事件订阅与「是否配置 TG / 企业微信」无关，必须始终建立**；
`running=True` 无条件设置；仅 `_set_commands()` / `_set_wecom_menu()` / 轮询线程 受
`has_tg` / `has_wecom` 精确门控。启动日志新增 `(event subscriptions active, tg=..., wecom=...)`。

回归测试：`tools/check_notification_bot_subscription.py`（纯标准库，29 断言，覆盖「仅启用 Bark」
场景下的入库事件订阅与 Bark 触发）。

### 5.2 v1.9.3 — Bark 正文改走「自定义通知模板」插件（需求增强）

原先 Bark 入库正文由 `bark_push.push_new_library_item()` 内部固定拼装，与管理端 TG /
企业微信 使用的风格模板不一致。现：

- `bark_push.push_new_library_item()` 新增 `body` 覆盖参数：传入则由调用方提供已渲染正文，
  保留 `notify_library_new` 开关判定与 `send()` 发送逻辑（向后兼容，原调用不受影响）；
- `notification_bot_library_new_item_service._bark_notify_new_item()` 与
  `notification_bot_library_new_episode_service._bark_notify_new_episode()` 先调用
  `notify_template` 插件渲染 `library_new_item` / `library_new_episode` 模板，
  再将渲染结果作为 `body` 传入 Bark。

效果：Bark 入库通知与 TG / 企业微信 使用**同一套风格模板**（默认 / 二次元 / 幽默 / 高冷 /
古风 / 自定义），文案一致、可随机文案。

---

## 6. 验证方法（Verification）

```bash
# (A) 订阅与 Bark 触发回归（纯标准库，无需联网）
python tools/check_notification_bot_subscription.py .
#     期望：passed: 29 failed: 0

# (B) Bark 模块回归（含 safe_push 转发、开关、掩码）
python tests/test_bark_push.py
#     期望：通过 84 / 失败 0

# (C) 容器内真实触发（参考步骤 4/5 后再入库一部影片，观察日志）
docker logs -f emby-pulse 2>&1 | grep -i "\[Bark\] 新片入库通知"
#     期望出现：INFO [Bark] 新片入库通知: 推送成功
```

---

## 7. 关键配置项（Config Reference）

| 配置项 | 位置 | 作用 |
| --- | --- | --- |
| `enabled` | 系统设置 → 通知 → Bark | Bark 总开关 |
| `device_key` | 同上 | Bark Device Key（敏感，接口掩码）|
| `server` | 同上 | Bark 服务器地址（需 http(s):// 开头）|
| `notify_library_new` | 同上 | 新片入库 Bark 开关（独立于「入库通知总开关」）|
| `notify_request_new` | 同上 | 新的求片 Bark 开关 |
| `notify_template` 插件 | 插件中心 | 自定义通知风格 / 自定义模板（影响 Bark+TG+企微 文案）|

---

## 8. 注意事项（Notes）

1. **不要暴露真实端口**：本项目经反代后，Emby 内部 `play_url`（`http://内网IP:端口/...`）
   可能把真实端口推送到第三方 Bark / 用户通知服务。因此**用户端**通知（需求 5）明确不附带网址；
   管理端 Bark 的「点击跳转」`url` 为可选项，若使用公网 Bark 服务建议留空以避免内网信息泄露。
2. **EventBus 静默**：后续任何「某事件不推送」的排查，第一步都是确认该事件是否有订阅者
   （步骤 5），而不是先怀疑推送通道本身。
3. **开关独立性**：Bark 入库推送只受 `enabled` + `notify_library_new` 两个开关控制，
   与「入库通知总开关」（`get_enable_library_notify`）解耦，二者任一关闭都不影响对方。
