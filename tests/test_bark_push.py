#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统级 Bark 推送（1.9.0）纯标准库行为回归。

不用 pytest 收集，直接运行：
    python tests/test_bark_push.py

覆盖：
- Bark 发送模块：开关、Device Key 掩码、请求体、失败兜底、两类消息文案；
- 配置层：管理端掩码 / 运行期原文 / 保存时掩码不覆盖真实 Key；
- 系统级通道不侵入 notify_rules 渠道列表。

用桩注入 app.core.config.cfg，避免拉起完整应用依赖。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_BARK_PATH = os.path.join(_REPO_ROOT, "app", "domains", "notifications", "bark_push.py")
_NCFG_PATH = os.path.join(_REPO_ROOT, "app", "infra", "config", "notification_settings.py")

PASSED = 0
FAILED = []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  " + name)
    else:
        FAILED.append(name + ("  " + detail if detail else ""))
        print("  FAIL  " + name + ("  " + detail if detail else ""))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bark = load_module("ep_bark_push_under_test", _BARK_PATH)


# ---------------------------------------------------------------------------
# 配置层桩
# ---------------------------------------------------------------------------
class FakeCfg:
    def __init__(self):
        self.config = {}
        self.saved = False

    def get(self, key, default=None):
        return self.config.get(key, default)

    def save(self):
        self.saved = True

    def set(self, key, value):
        self.config[key] = value


FAKE_CFG = FakeCfg()


def install_cfg_stub():
    for name in ("app", "app.core", "app.infra", "app.infra.config"):
        if name not in sys.modules:
            m = types.ModuleType(name)
            m.__path__ = []
            sys.modules[name] = m
    core_config = types.ModuleType("app.core.config")
    core_config.cfg = FAKE_CFG
    sys.modules["app.core.config"] = core_config
    sys.modules["app.core"].config = core_config


install_cfg_stub()
ncfg = load_module("ep_notification_settings_under_test", _NCFG_PATH)


# ---------------------------------------------------------------------------
# Bark transport 桩
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, raw=b'{"code":200,"message":"ok"}', status=200):
        self._raw = raw
        self.status = status

    def read(self):
        return self._raw

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeTransport:
    def __init__(self, response=None, exc=None):
        self.response = response or FakeResponse()
        self.exc = exc
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append((req, timeout))
        if self.exc:
            raise self.exc
        return self.response

    @property
    def last_payload(self):
        if not self.calls:
            return {}
        return json.loads(self.calls[-1][0].data.decode("utf-8"))

    @property
    def last_url(self):
        if not self.calls:
            return ""
        return self.calls[-1][0].full_url


BASE_CONF = {
    "enabled": True,
    "server": "https://push.example.com",
    "device_key": "REAL_DEVICE_KEY_123456",
    "sound": "default",
    "group": "EMBYPULSE",
    "level": "active",
    "notify_library_new": True,
    "notify_request_new": True,
}


def conf(**overrides):
    c = dict(BASE_CONF)
    c.update(overrides)
    return c


print("== 1. 配置层：掩码 / 原文 / 保存护栏 ==")
FAKE_CFG.config.clear()
FAKE_CFG.config["bark_device_key"] = "ABCD1234EFGH5678"
masked = ncfg.get_bark_config(masked=True)
runtime = ncfg.get_bark_runtime_config()
check("管理端读取 Device Key 做掩码", "****" in masked["device_key"], str(masked))
check("掩码不泄露完整 Key", masked["device_key"] != "ABCD1234EFGH5678")
check("运行期读取 Device Key 原文", runtime["device_key"] == "ABCD1234EFGH5678")
check("默认服务器为官方 Bark 服务", masked["server"] == "https://api.day.app")
check("默认总开关关闭", masked["enabled"] is False)
check("两类消息默认开启",
      masked["notify_library_new"] is True and masked["notify_request_new"] is True)
check("默认分组 / 铃声", masked["group"] == "EMBYPULSE" and masked["sound"] == "default")

ncfg.set_bark_config({
    "enabled": True,
    "server": "https://bark.example.com/",
    "device_key": masked["device_key"],   # 回填掩码值
    "notify_library_new": False,
})
check("掩码值不覆盖已存真实 Key",
      FAKE_CFG.config["bark_device_key"] == "ABCD1234EFGH5678")
check("服务器尾斜杠被清理", FAKE_CFG.config["bark_server"] == "https://bark.example.com")
check("总开关写入生效", FAKE_CFG.config["bark_enabled"] is True)
check("消息开关写入生效", FAKE_CFG.config["bark_notify_library_new"] is False)
check("保存配置调用 cfg.save()", FAKE_CFG.saved is True)

ncfg.set_bark_config({"device_key": "NEW_REAL_KEY_9999"})
check("真实 Key 可以正常更新", FAKE_CFG.config["bark_device_key"] == "NEW_REAL_KEY_9999")
ncfg.set_bark_config(None)
check("非 dict 配置安全忽略", FAKE_CFG.config["bark_device_key"] == "NEW_REAL_KEY_9999")

channels_masked = ncfg.get_notification_channels_config()
check("渠道配置暴露 bark 节点", "bark" in channels_masked)
check("渠道配置里 bark Key 掩码", "****" in channels_masked["bark"]["device_key"])
check("Bark 未混入 notify_rules 渠道列表",
      "bark" not in (ncfg.get_notify_channels() or ""))
check("运行期渠道配置 Key 为原文",
      ncfg.get_notification_channels_runtime_config()["bark"]["device_key"]
      == "NEW_REAL_KEY_9999")

print("== 2. send()：开关 / 校验 / 请求体 / 兜底 ==")
t = FakeTransport()
r = bark.send("标题", "正文", config=conf(enabled=False), transport=t)
check("总开关关闭时跳过且不发请求", r["skipped"] is True and not t.calls)
check("跳过时返回 ok=True（不影响业务）", r["ok"] is True)

t = FakeTransport()
r = bark.send("标题", "正文", config=conf(device_key=""), transport=t)
check("缺少 Device Key 时跳过", r["skipped"] is True and not t.calls)
check("缺 Key 明确返回失败原因", "Device Key" in r["detail"], str(r))

t = FakeTransport()
r = bark.send("标题", "正文", config=conf(server="not-a-url"), transport=t)
check("非法服务器地址时不发请求", r["skipped"] is True and not t.calls)
check("非法地址提示 http(s)", "http" in r["detail"], str(r))

t = FakeTransport()
r = bark.send("新片入库", "正文内容", url="https://emby.example.com/play",
             config=conf(), transport=t)
check("正常推送返回 ok=True", r["ok"] is True, str(r))
check("POST 到 {server}/push", t.last_url == "https://push.example.com/push", t.last_url)
p = t.last_payload
check("请求体携带 device_key", p.get("device_key") == "REAL_DEVICE_KEY_123456")
check("请求体标题正确", p.get("title") == "新片入库")
check("请求体正文正确", p.get("body") == "正文内容")
check("请求体携带跳转 url", p.get("url") == "https://emby.example.com/play")
check("请求体携带 group/sound/level",
      p.get("group") == "EMBYPULSE" and p.get("sound") == "default"
      and p.get("level") == "active")
check("超时参数已设置", t.calls[-1][1] == bark.BARK_TIMEOUT)
check("返回值不含 device_key", "device_key" not in r)
check("返回的 payload 不含 device_key", "device_key" not in r.get("payload", {}))
check("返回体携带 endpoint", r.get("endpoint") == "https://push.example.com/push")

t = FakeTransport()
r = bark.send("T", "B", group="自定义组", sound="alarm", level="timeSensitive",
              config=conf(), transport=t)
p = t.last_payload
check("调用方可覆盖 group/sound/level",
      p.get("group") == "自定义组" and p.get("sound") == "alarm"
      and p.get("level") == "timeSensitive")

t = FakeTransport()
r = bark.send("T" * 500, "B" * 5000, url="https://x.example.com/" + "u" * 900,
              config=conf(), transport=t)
p = t.last_payload
check("标题长度截断", len(p["title"]) == bark._TITLE_MAX)
check("正文长度截断", len(p["body"]) == bark._BODY_MAX)
check("跳转 URL 长度截断", len(p["url"]) == bark._URL_MAX)

t = FakeTransport(response=FakeResponse(status=500))
r = bark.send("标题", "正文", config=conf(), transport=t)
check("HTTP 非 2xx 判定失败", r["ok"] is False, str(r))
check("HTTP 失败不抛异常且返回 detail", "HTTP 500" in r["detail"])

t = FakeTransport(response=FakeResponse(raw=b'{"code":400,"message":"bad device_key"}'))
r = bark.send("标题", "正文", config=conf(), transport=t)
check("Bark code 非 200 判定失败", r["ok"] is False, str(r))

t = FakeTransport(response=FakeResponse(raw=b"not-json", status=200))
r = bark.send("标题", "正文", config=conf(), transport=t)
check("非 JSON 响应仍按 HTTP 状态判定成功", r["ok"] is True, str(r))

t = FakeTransport(exc=OSError("network down"))
r = bark.send("标题", "正文", config=conf(), transport=t)
check("网络异常被吞掉不抛出", r["ok"] is False and r["skipped"] is False)
check("异常信息进入 detail", "network down" in r["detail"], str(r))
check("异常返回体也不含 device_key", "device_key" not in r.get("payload", {}))

print("== 3. 新片入库通知 ==")
t = FakeTransport()
r = bark.push_new_library_item(name="沙丘2", item_type="Movie", year="2024",
                               rating="8.1", overview="保罗的复仇之路",
                               play_url="https://emby.example.com/item/1",
                               config=conf(), transport=t)
p = t.last_payload
check("新片入库标题为「新片入库」", p.get("title") == "新片入库")
check("新片入库正文含片名", "沙丘2" in p.get("body", ""))
check("新片入库识别为电影", "电影" in p.get("body", ""))
check("新片入库含年份与评分", "2024" in p.get("body", "") and "8.1" in p.get("body", ""))
check("新片入库含简介", "保罗的复仇之路" in p.get("body", ""))
check("新片入库携带播放地址", p.get("url") == "https://emby.example.com/item/1")

t = FakeTransport()
r = bark.push_new_library_item(series_name="权力的游戏", episode_info="S01E02",
                               item_type="Episode", config=conf(), transport=t)
p = t.last_payload
check("剧集入库使用剧集名", "权力的游戏" in p.get("body", ""))
check("剧集入库带集数后缀", "S01E02" in p.get("body", ""))
check("剧集入库识别为剧集", "剧集" in p.get("body", ""))

for bad_rating in ("N/A", "None", ""):
    t = FakeTransport()
    bark.push_new_library_item(name="某影片", rating=bad_rating,
                               config=conf(), transport=t)
    check("无效评分 %r 不写入正文" % bad_rating,
          "评分：" not in t.last_payload.get("body", ""))

t = FakeTransport()
bark.push_new_library_item(name="某影片", rating="9.0", config=conf(), transport=t)
check("有效评分正常写入", "评分：9.0" in t.last_payload.get("body", ""))

t = FakeTransport()
r = bark.push_new_library_item(name="X", config=conf(notify_library_new=False),
                               transport=t)
check("新片入库开关关闭时跳过", r["skipped"] is True and not t.calls)

t = FakeTransport()
r = bark.push_new_library_item(name="X", config=conf(enabled=False), transport=t)
check("总开关关闭时新片入库跳过", r["skipped"] is True and not t.calls)

print("== 4. 新的求片通知 ==")
t = FakeTransport()
r = bark.push_new_request(title="星际穿越", year="2014", user="alice",
                          media_type="movie", admin_url="https://pulse.example.com/requests_admin",
                          config=conf(), transport=t)
p = t.last_payload
check("求片通知标题为「新的求片」", p.get("title") == "新的求片")
check("求片通知含片名与年份", "星际穿越" in p.get("body", "") and "2014" in p.get("body", ""))
check("求片通知含提交用户", "alice" in p.get("body", ""))
check("求片通知识别为电影", "电影" in p.get("body", ""))
check("求片通知含审批提示", "审批" in p.get("body", ""))
check("求片通知携带后台地址", p.get("url") == "https://pulse.example.com/requests_admin")

t = FakeTransport()
r = bark.push_new_request(title="某剧", user="bob", media_type="tv",
                          seasons=[1, 2, "bad", 0, 3], config=conf(), transport=t)
p = t.last_payload
check("剧集求片识别为剧集", "剧集" in p.get("body", ""))
check("剧集求片合并季数", "第 1,2,3 季" in p.get("body", ""), str(p.get("body")))
check("非法季数被过滤", "bad" not in p.get("body", "") and "第 1,2,3,0 季" not in p.get("body", ""))

t = FakeTransport()
r = bark.push_new_request(title="X", config=conf(notify_request_new=False),
                          transport=t)
check("求片开关关闭时跳过", r["skipped"] is True and not t.calls)

print("== 5. 测试推送 / safe_push 兜底 ==")
t = FakeTransport()
r = bark.send_test(config=conf(enabled=False, notify_library_new=False,
                               notify_request_new=False), transport=t)
check("测试推送绕过所有开关", len(t.calls) == 1 and r["ok"] is True, str(r))
check("测试推送标题明确", t.last_payload.get("title") == "EMBYPULSE 测试推送")

t = FakeTransport()
r = bark.send_test(config=conf(enabled=False, device_key=""), transport=t)
check("测试推送缺 Key 仍不发请求", not t.calls and r["skipped"] is True)

r = bark.safe_push("unknown_kind", title="X")
check("safe_push 未知类型安全返回", r["ok"] is False and r["skipped"] is True)

t = FakeTransport()
r = bark.safe_push("library_new", name="测试片", config=conf(), transport=t)
check("safe_push 正常转发新片入库", len(t.calls) == 1 and r["ok"] is True)

t = FakeTransport()
r = bark.safe_push("request_new", title="测试求片", config=conf(), transport=t)
check("safe_push 正常转发新的求片", len(t.calls) == 1 and r["ok"] is True)

r = bark.safe_push("library_new", unexpected_kwarg=123, config=conf())
check("safe_push 吞掉参数异常", r["ok"] is False and r["skipped"] is True, str(r))

print("== 6. Webhook ?token= 兼容（后端行为） ==")
_WEBHOOK_PATH = os.path.join(_REPO_ROOT, "app", "domains", "system", "webhook.py")
import ast

with open(_WEBHOOK_PATH, encoding="utf-8") as f:
    _webhook_src = f.read()
_tree = ast.parse(_webhook_src)
# 只抽出 `import os` 与目标函数，避免 exec 时拉起 fastapi / 应用依赖
_nodes = [n for n in _tree.body
          if isinstance(n, ast.Import) and any(a.name == "os" for a in n.names)]
_nodes += [n for n in _tree.body
           if isinstance(n, ast.FunctionDef) and n.name == "_get_webhook_token"]
_ns = {"Request": object, "os": os}
exec(compile(ast.Module(body=_nodes, type_ignores=[]), "<webhook-check>", "exec"), _ns)
_get_token = _ns["_get_webhook_token"]


class FakeRequest:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query_params = query or {}


os.environ.pop("WEBHOOK_ALLOW_QUERY_TOKEN", None)
check("默认兼容 Emby ?token= URL",
      _get_token(FakeRequest({}, {"token": "MY_TOKEN"})) == "MY_TOKEN")
check("无 token 时返回 None", _get_token(FakeRequest()) is None)
check("请求头优先于 URL 参数",
      _get_token(FakeRequest({"X-Webhook-Token": "HEADER"}, {"token": "QUERY"})) == "HEADER")
os.environ["WEBHOOK_ALLOW_QUERY_TOKEN"] = "0"
check("显式 0 回到严格 Header-only",
      _get_token(FakeRequest({}, {"token": "MY_TOKEN"})) is None)
check("严格模式仍接受请求头",
      _get_token(FakeRequest({"X-Webhook-Token": "HEADER"})) == "HEADER")
os.environ["WEBHOOK_ALLOW_QUERY_TOKEN"] = "1"
check("显式 1 仍兼容 URL",
      _get_token(FakeRequest({}, {"token": "MY_TOKEN"})) == "MY_TOKEN")
os.environ.pop("WEBHOOK_ALLOW_QUERY_TOKEN", None)

print("== 7. 1.9.7：channels 门控 / 管理端通知设置真开关 ==")
# 让 bark_push 内的局部 import 命中本文件的配置层桩，不拉起真实应用依赖
for _name in ("app.domains", "app.domains.notifications"):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        _m.__path__ = []
        sys.modules[_name] = _m
sys.modules["app.infra.config.notification_settings"] = ncfg
_dao_stub = types.ModuleType("app.domains.notifications.notify_admin_dao")
_dao_stub.get_notify_rule_row = lambda t: None
sys.modules["app.domains.notifications.notify_admin_dao"] = _dao_stub

FAKE_CFG.config.clear()
FAKE_CFG.config.update({
    "bark_enabled": False,
    "bark_server": "https://push.example.com",
    "bark_device_key": "REAL_DEVICE_KEY_123456",
})

# 7.1 总控未勾 Bark 且卡片未启用 -> 不推
FAKE_CFG.config["library_notify_channels"] = '["tg_bot","wecom"]'
t = FakeTransport()
r = bark.safe_push("library_new", name="沙丘", transport=t)
check("总控未勾 Bark 且卡片未启用时入库不推",
      r["skipped"] is True and not t.calls, str(r))

# 7.2 借鉴企微：勾选渠道即合法启用来源，即使卡片 enabled=False 也推
FAKE_CFG.config["library_notify_channels"] = '["tg_bot","bark"]'
t = FakeTransport()
r = bark.safe_push("library_new", name="沙丘", transport=t)
check("总控勾 Bark 后入库真实发出请求",
      len(t.calls) == 1 and r["ok"] is True, str(r))
check("渠道驱动时请求体仍携带正确 Device Key",
      t.last_payload.get("device_key") == "REAL_DEVICE_KEY_123456")

# 7.3 入库总控的勾选不得串扰求片事件
t = FakeTransport()
r = bark.safe_push("request_new", title="星际穿越", transport=t)
check("入库总控勾 Bark 不串扰求片通知",
      r["skipped"] is True and not t.calls, str(r))

# 7.4 管理端浮窗：未勾跳过；勾了真推；规则禁用时不推
_dao_stub.get_notify_rule_row = lambda t: {"enabled": 1, "channels": '["tg_bot","wecom"]'}
t = FakeTransport()
r = bark.push_rule_notification("feedback_new", "用户报错", "测试", transport=t)
check("浮窗未勾 Bark 时通知规则跳过",
      r["skipped"] is True and not t.calls, str(r))

_dao_stub.get_notify_rule_row = lambda t: {"enabled": 1, "channels": '["tg_bot","bark"]'}
t = FakeTransport()
r = bark.safe_rule_push("feedback_new", "用户报错",
                        "<b>资源报错</b>：测试", transport=t)
check("浮窗勾 Bark 后真实发出请求",
      len(t.calls) == 1 and r["ok"] is True, str(r))
check("Bark 正文剥离 Telegram HTML 标签",
      "<b>" not in t.last_payload.get("body", "")
      and "资源报错" in t.last_payload.get("body", ""))

_dao_stub.get_notify_rule_row = lambda t: {"enabled": 0, "channels": '["bark"]'}
t = FakeTransport()
r = bark.push_rule_notification("user_login", "账号登录", "测试", transport=t)
check("规则被禁用时即使勾了 Bark 也不推",
      r["skipped"] is True and not t.calls, str(r))

# 7.5 显式注入 config 的调用（测试 / 用户级推送）不受门控影响
FAKE_CFG.config["library_notify_channels"] = '["bark"]'
t = FakeTransport()
r = bark.safe_push("library_new", name="X", config=conf(enabled=False), transport=t)
check("注入 config 时沿用调用方开关，不受 channels 门控影响",
      r["skipped"] is True and not t.calls, str(r))

print("\n通过 %d / 失败 %d" % (PASSED, len(FAILED)))
if FAILED:
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("BARK PUSH + WEBHOOK TOKEN CHECKS PASSED")
