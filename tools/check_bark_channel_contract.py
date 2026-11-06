#!/usr/bin/env python3
"""校验「推送场景总控 → Bark 推送」前后端字段契约（1.9.5）。

背景：1.9.5 把 Bark 配置卡片新增到机器人助手页（templates/bot.html），
保存走 POST /api/notify/channels_config。前端字段名与后端
bark_push.py / set_bark_config 期望字段一旦不一致，保存会静默丢字段，
表现为「配置了但不推送」。本脚本用正则 + AST 做静态比对，不依赖应用依赖。

用法: python tools/check_bark_channel_contract.py [ROOT]
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
FAILS = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  " + name)
    else:
        FAILS.append(name + ("  " + detail if detail else ""))
        print("  FAIL  " + name + ("  " + detail if detail else ""))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


bot_tpl = read("templates/bot.html")
bark = read("app/domains/notifications/bark_push.py")
ncfg = read("app/infra/config/notification_settings.py")
nadmin = read("app/domains/notifications/notify_admin.py")

# ---- 1. 前端 collectBotBarkSettings 提交的字段 ----
m = re.search(r"function collectBotBarkSettings\(\)\s*\{(.*?)\n\s*\}", bot_tpl, re.S)
check("bot.html 存在 collectBotBarkSettings", m is not None)
front_fields = set()
if m:
    body = m.group(1)
    front_fields = set(re.findall(r"^\s{16}(\w+):", body, re.M))
check("前端提交 bark 节点七个字段",
      front_fields == {"enabled", "server", "device_key", "sound", "group",
                       "notify_library_new", "notify_request_new"},
      "实际: %s" % sorted(front_fields))

# ---- 2. 后端 bark_push.py 读取的字段 ----
back_fields = set(re.findall(r'conf\.get\("(\w+)"', bark))
# 事件开关经 _event_allowed(conf, "notify_library_new") 读取，补两种形式
back_fields |= set(re.findall(r'_event_allowed\(conf,\s*"(\w+)"', bark))
need_back = {"enabled", "server", "device_key", "sound", "group",
             "notify_library_new", "notify_request_new"}
check("后端读取字段覆盖前端提交字段", need_back <= back_fields,
      "后端: %s" % sorted(back_fields))

# ---- 3. 保存链路：channels_config -> set_bark_config -> cfg ----
check("channels_config 保存入口存在",
      '@router.get("/api/notify/channels_config")' in nadmin
      and '@router.post("/api/notify/channels_config")' in nadmin)
check("保存时只更新 bark 节点",
      'if "bark" in data:' in ncfg and 'set_bark_config(data["bark"])' in ncfg)
check("掩码值不覆盖已存 Device Key", '"****" not in key' in ncfg)
check("set_bark_config 写入运行期配置", "def set_bark_config(data: dict) -> None:" in ncfg)
check("get_bark_runtime_config 可读回", "def get_bark_runtime_config() -> dict:" in ncfg)

# ---- 4. 入库推送门控：Bark 由自身开关控制，先于入库通知总开关 ----
nitem = read("app/bot/notification_bot/notification_bot_library_new_item_service.py")
body = nitem.split("def handle_library_new_item(", 1)[1]
check("电影入库 handler 调用 Bark 推送",
      "_bark_notify_new_item(item)" in body)
check("Bark 调用位于入库通知总开关判断之前",
      body.index("_bark_notify_new_item(item)")
      < body.index("if not _enable_library_notify_provider()():"))
check("入库事件接 Bark（request_new 同类事件开关存在）",
      '"library_new"' in nitem)

# ---- 5. 卡片 UI 控件与 save/test 函数一一对应 ----
for cid in ("bot-bark-enabled", "bot-bark-server", "bot-bark-device-key",
            "bot-bark-notify-library-new", "bot-bark-notify-request-new",
            "bot-bark-sound", "bot-bark-group"):
    check("卡片控件 %s 存在" % cid, 'id="%s"' % cid in bot_tpl)
for fn in ("loadBotBarkConfig", "saveBotBarkSettings", "testBotBarkSettings",
           "collectBotBarkSettings"):
    check("卡片函数 %s 已定义" % fn, ("function %s(" % fn) in bot_tpl or ("async function %s(" % fn) in bot_tpl)
check("保存成功后重读配置（Device Key 显示掩码）",
      "await loadBotBarkConfig()" in bot_tpl)
check("loadConfig 中调用 loadBotBarkConfig", "loadBotBarkConfig();" in bot_tpl)

# ---- 6. 通知渠道勾选包含 bark（不影响 tg/wecom 识别） ----
check("渠道勾选含 notify_channel_bark", 'id="notify_channel_bark"' in bot_tpl)
check("保存渠道含 bark", "['bark']" in bot_tpl)
check("回填渠道含 bark", "libChannels.includes('bark')" in bot_tpl)

# ---- 7. 1.9.7：入库 Bark 真实断点修复 + 管理端真开关 ----
check("顶部保存配置一并提交 Bark 卡片",
      "await saveBotBarkSettings({silent: true})" in bot_tpl)
check("Bark 保存函数返回成败供顶部保存判定",
      "return ok;" in bot_tpl and "const silent" in bot_tpl)
check("bark_push 按事件类型应用 channels 门控",
      "_RULE_TYPE_BY_KIND" in bark and "def _apply_channel_gate(" in bark)
check("bark_push 实现通知规则推送入口",
      "def push_rule_notification(" in bark and "def safe_rule_push(" in bark)
check("未推送原因写进日志（可自证缺 Key / 未启用 / 地址非法）",
      "未推送原因" in bark)
check("配置层不引入 BARK_* env，不混淆 WEBHOOK_TOKEN",
      "BARK_ENV_FIELDS" not in ncfg and "os.getenv" not in ncfg
      and "WEBHOOK_TOKEN" in ncfg)
check("is_bark_channel_selected 按 notify_type 精确判断",
      "def is_bark_channel_selected(notify_type" in ncfg)
check("CHANNEL_OPTIONS 含 Bark 渠道", '{"id": "bark"' in nadmin)

req_admin_tpl = read("templates/requests_admin.html")
users_tpl = read("templates/users.html")
for _t in ("request_new", "request_update", "request_status", "feedback_new"):
    check("工单大厅 %s 有 Bark 勾选" % _t,
          'data-type="%s" data-channel="bark"' % _t in req_admin_tpl)
for _t in ("user_register", "user_login", "user_delete"):
    check("用户管理 %s 有 Bark 勾选" % _t,
          'data-type="%s" data-channel="bark"' % _t in users_tpl)

for _p in ("app/domains/media_requests/feedback_router.py",
           "app/domains/media_requests/registration_router.py",
           "app/domains/users/delete_router.py",
           "app/bot/notification_bot/notification_bot_user_login_service.py",
           "app/domains/media_requests/update_router.py",
           "app/domains/media_requests/management_router.py"):
    check("触发点 %s 真实消费 Bark 渠道" % _p.split("/")[-1],
          "safe_rule_push(" in read(_p))

print("\npassed: %d  failed: %d" % (PASSED, len(FAILS)))
if FAILS:
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("BARK CHANNEL CONTRACT CHECKS PASSED")
