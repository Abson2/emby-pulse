# -*- coding: utf-8 -*-
"""插件中心「定时执行」cron 计划（2.5.1）。

设计约束（与截图交互一致）：

* **北京时间 UTC+8**：cron 5 段（分 时 日 月 周）按 Asia/Shanghai 固定偏移解释，
  页面展示的「下次执行 / 最近触发」均为 UTC+8，不受容器 TZ 影响；
* **上一轮仍在运行或排队时跳过本次触发**：``PlanRuntime`` 维护 running 集合，
  cron 与手动「立即执行」共用，跳过时写一条 ``skipped`` 结果并把下次执行后移；
* **服务离线期间不补跑**：启动时 ``reconcile()`` 把所有过期计划直接推进到
  「当前时间之后的下一次」，不回溯执行遗漏的触发；
* **纯标准库**：顶层只依赖标准库，可被纯标准库契约测试用 ``runpy`` 独立加载；
  插件执行逻辑通过注入的 ``executor`` 回调，本模块不感知 FastAPI / 数据库；
* 计划落盘到持久化目录（默认 ``/workspace/data/plugin_schedules.json``，
  对应 compose 的 ``./data`` 卷），原子写 + 进程内锁，重启不丢。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

# 北京时间固定 UTC+8（中国自 1991 年后无夏令时，固定偏移即可）
TZ_BEIJING = timezone(timedelta(hours=8))
DEFAULT_PLANS_FILE = "/workspace/data/plugin_schedules.json"
ENV_PLANS_FILE = "PLUGIN_PLANS_FILE"

PLAN_ID_RE = re.compile(r"^plan_[a-z0-9]{8,16}$")
PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
# 与 external.py 一致：文本字段拒绝 HTML 特殊字符，避免插件中心模板注入
TEXT_FORBIDDEN = frozenset("<>&\"'`\\\x00")
MAX_TEXT_LEN = 64
MAX_DESC_LEN = 128
MAX_CRON_LEN = 64
MAX_RESULTS = 20

_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_FIELD_NAMES = ("分钟", "小时", "日", "月", "周")
_DOW_ALIASES = {
    "sun": "0", "mon": "1", "tue": "2", "wed": "3",
    "thu": "4", "fri": "5", "sat": "6",
}


class PlanError(ValueError):
    """计划校验失败；消息可直接回显给管理员，不含敏感信息。"""


# ---------------------------------------------------------------- cron 解析


def _parse_field(token: str, lo: int, hi: int, label: str, is_dow: bool = False):
    """解析单个 cron 字段为允许值集合。支持 * / a / a-b / a,b / */n / a-b/n。"""
    allowed = set()
    if not token or len(token) > 40:
        raise PlanError("%s 字段为空或过长" % label)
    # 整个字段是否为通配（决定 cron 的「日 / 周」OR 语义）；*/n 属于限定
    star = token.strip() == "*"
    for part in token.split(","):
        part = part.strip()
        if not part:
            raise PlanError("%s 字段含空项" % label)
        if is_dow:
            part = _DOW_ALIASES.get(part.lower(), part)
        step = 1
        if part.startswith("*/"):
            start, end = lo, hi
            step = _parse_int(part[2:], label)
        elif "/" in part:
            base, _, step_raw = part.partition("/")
            step = _parse_int(step_raw, label)
            if "-" in base:
                a, _, b = base.partition("-")
                start, end = _parse_int(a, label), _parse_int(b, label)
            else:
                start = _parse_int(base, label)
                end = hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = _parse_int(a, label), _parse_int(b, label)
        elif part == "*":
            start, end = lo, hi
        else:
            start = _parse_int(part, label)
            end = start
        if step <= 0 or step > hi - lo + 1:
            raise PlanError("%s 步长非法: %s" % (label, part))
        if start < lo or end > hi or start > end:
            raise PlanError("%s 取值越界: %s（允许 %d~%d）" % (label, part, lo, hi))
        for value in range(start, end + 1, step):
            # cron 星期 0 与 7 都表示周日，统一归一到 0
            allowed.add(0 if (is_dow and value == 7) else value)
    return frozenset(allowed), star


def _parse_int(raw: str, label: str) -> int:
    raw = (raw or "").strip()
    if not raw or not raw.isdigit():
        raise PlanError("%s 含非法数字: %s" % (label, raw))
    return int(raw)


class CronSpec:
    """5 段 cron（分 时 日 月 周），按北京时间匹配。"""

    __slots__ = ("text", "fields", "dom_star", "dow_star")

    def __init__(self, expr: str):
        if not isinstance(expr, str) or not expr.strip():
            raise PlanError("cron 表达式不能为空")
        expr = expr.strip()
        if len(expr) > MAX_CRON_LEN:
            raise PlanError("cron 表达式过长（上限 %d 字符）" % MAX_CRON_LEN)
        parts = expr.split()
        if len(parts) != 5:
            raise PlanError("cron 需 5 段（分 时 日 月 周），当前 %d 段" % len(parts))
        parsed = []
        for i, (token, (lo, hi)) in enumerate(zip(parts, _FIELD_RANGES)):
            parsed.append(_parse_field(token, lo, hi, _FIELD_NAMES[i], is_dow=(i == 4)))
        self.text = expr
        self.fields = tuple(p[0] for p in parsed)
        self.dom_star = parsed[2][1]
        self.dow_star = parsed[4][1]
        # 表达式必须至少存在一个可匹配时刻，否则会无限推进
        if not self.fields[0] or not self.fields[1] or not self.fields[3]:
            raise PlanError("cron 分钟/小时/月 字段不能为空集合")

    def _day_matches(self, dt: datetime) -> bool:
        """标准 cron 语义：日与周都被限定时取 OR；仅一个被限定时按该字段；都为 * 则恒真。"""
        dom_ok = dt.day in self.fields[2]
        # Python weekday(): Mon=0..Sun=6 -> cron: Sun=0..Sat=6
        dow_ok = ((dt.weekday() + 1) % 7) in self.fields[4]
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok

    def matches(self, dt: datetime) -> bool:
        return (dt.minute in self.fields[0]
                and dt.hour in self.fields[1]
                and dt.month in self.fields[3]
                and self._day_matches(dt))

    def next_after(self, dt: datetime) -> datetime | None:
        """返回 dt 之后（不含 dt 所在分钟）第一个匹配的北京时间分钟；最多找 366 天。"""
        cur = dt.astimezone(TZ_BEIJING).replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = cur + timedelta(days=366)
        while cur <= end:
            if cur.month not in self.fields[3] or not self._day_matches(cur):
                cur = (cur + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if cur.hour not in self.fields[1]:
                cur = (cur + timedelta(hours=1)).replace(minute=0)
                continue
            if cur.minute not in self.fields[0]:
                cur += timedelta(minutes=1)
                continue
            return cur
        return None


def parse_cron(expr: str) -> CronSpec:
    return CronSpec(expr)


def next_ts(expr: str, after_ts: float) -> float | None:
    """cron 在 after_ts 之后的下一次触发（epoch 秒）。"""
    spec = parse_cron(expr)
    dt = datetime.fromtimestamp(after_ts, TZ_BEIJING)
    nxt = spec.next_after(dt)
    return nxt.timestamp() if nxt is not None else None


def fmt_ts(ts: float | None) -> str:
    """北京时间友好显示，与截图一致：2026/11/08 09:00。"""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), TZ_BEIJING).strftime("%Y/%m/%d %H:%M")
    except (OSError, OverflowError, ValueError, TypeError):
        return ""


# ---------------------------------------------------------------- 校验 / 存储


def _clean_text(value, field: str, max_len: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PlanError("%s 必须是文本" % field)
    value = value.strip()
    if required and not value:
        raise PlanError("%s 不能为空" % field)
    if len(value) > max_len:
        raise PlanError("%s 过长（上限 %d 字）" % (field, max_len))
    if any(ch in TEXT_FORBIDDEN for ch in value):
        raise PlanError("%s 含非法字符" % field)
    return value


def validate_plan_input(data: dict, *, partial: bool = False) -> dict:
    """校验并规范化新增/编辑字段；partial=True 时只校验出现的字段。"""
    if not isinstance(data, dict):
        raise PlanError("请求数据格式错误")
    out = {}

    def present(key):
        return (not partial) or (key in data)

    if present("name"):
        out["name"] = _clean_text(data.get("name"), "计划名称", MAX_TEXT_LEN)
    if present("description"):
        out["description"] = _clean_text(
            data.get("description"), "计划描述", MAX_DESC_LEN, required=False)
    if present("plugin_id"):
        pid = str(data.get("plugin_id") or "").strip()
        if not PLUGIN_ID_RE.match(pid):
            raise PlanError("插件 ID 非法")
        out["plugin_id"] = pid
    if present("cron"):
        cron = str(data.get("cron") or "").strip()
        # 预解析，非法表达式在写入前就被拒绝
        parse_cron(cron)
        out["cron"] = cron
    if "enabled" in data:
        out["enabled"] = bool(data.get("enabled"))
    elif not partial:
        out["enabled"] = True
    return out


class PlanStore:
    """计划 JSON 存储：原子写 + 重入锁 + 损坏文件容错。"""

    def __init__(self, path: str | None = None, clock=time.time):
        self._path = path or os.environ.get(ENV_PLANS_FILE, "").strip() or DEFAULT_PLANS_FILE
        self._clock = clock
        self._lock = threading.RLock()
        self._data = {"version": 1, "plans": []}
        self._load()

    # ---- 底层 IO ----
    @property
    def path(self) -> str:
        return self._path

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
            plans = raw.get("plans") if isinstance(raw, dict) else None
            self._data = {"version": 1, "plans": plans if isinstance(plans, list) else []}
        except (OSError, ValueError, TypeError):
            self._data = {"version": 1, "plans": []}

    def _save(self) -> None:
        folder = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, ".plans-%s.tmp" % uuid.uuid4().hex)
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._data, fp, ensure_ascii=False, indent=2)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _find(self, plan_id: str):
        for plan in self._data["plans"]:
            if plan.get("id") == plan_id:
                return plan
        return None

    @staticmethod
    def _clone(plan: dict) -> dict:
        return json.loads(json.dumps(plan))

    # ---- CRUD ----
    def list_plans(self) -> list[dict]:
        with self._lock:
            return [self._clone(p) for p in self._data["plans"]]

    def get_plan(self, plan_id: str) -> dict | None:
        with self._lock:
            found = self._find(plan_id)
            return self._clone(found) if found else None

    def create_plan(self, data: dict) -> dict:
        clean = validate_plan_input(data)
        now = self._clock()
        plan = {
            "id": "plan_%s" % uuid.uuid4().hex[:12],
            "name": clean["name"],
            "description": clean.get("description", ""),
            "plugin_id": clean["plugin_id"],
            "cron": clean["cron"],
            "enabled": clean.get("enabled", True),
            "created_at": now,
            "updated_at": now,
            "next_run_at": next_ts(clean["cron"], now),
            "last_run_at": None,
            "last_status": "",
            "last_message": "",
            "last_duration_ms": 0,
            "results": [],
        }
        if plan["next_run_at"] is None:
            raise PlanError("cron 在未来一年内无可执行时刻")
        with self._lock:
            self._data["plans"].append(plan)
            self._save()
        return self._clone(plan)

    def update_plan(self, plan_id: str, patch: dict) -> dict | None:
        clean = validate_plan_input(patch, partial=True)
        with self._lock:
            plan = self._find(plan_id)
            if plan is None:
                return None
            cron_changed = "cron" in clean and clean["cron"] != plan["cron"]
            reenabled = clean.get("enabled") is True and not plan.get("enabled")
            plan.update(clean)
            plan["updated_at"] = self._clock()
            # cron 变更或重新启用时，从「现在」重算下次执行，不补跑离线期间
            if cron_changed or reenabled or plan.get("next_run_at") is None:
                plan["next_run_at"] = next_ts(plan["cron"], self._clock())
                if plan["next_run_at"] is None:
                    raise PlanError("cron 在未来一年内无可执行时刻")
            self._save()
            return self._clone(plan)

    def delete_plan(self, plan_id: str) -> bool:
        with self._lock:
            before = len(self._data["plans"])
            self._data["plans"] = [p for p in self._data["plans"] if p.get("id") != plan_id]
            changed = len(self._data["plans"]) != before
            if changed:
                self._save()
            return changed

    def record_result(self, plan_id: str, status: str, message: str,
                      duration_ms: int = 0, trigger: str = "cron") -> None:
        """写一条运行结果并更新计划的「最近触发」摘要；保留最近 MAX_RESULTS 条。"""
        now = self._clock()
        with self._lock:
            plan = self._find(plan_id)
            if plan is None:
                return
            entry = {
                "at": now,
                "status": status,
                "message": (message or "")[:MAX_DESC_LEN],
                "duration_ms": max(0, int(duration_ms or 0)),
                "trigger": trigger,
            }
            results = plan.setdefault("results", [])
            results.insert(0, entry)
            del results[MAX_RESULTS:]
            plan["last_run_at"] = now
            plan["last_status"] = status
            plan["last_message"] = entry["message"]
            plan["last_duration_ms"] = entry["duration_ms"]
            self._save()

    def results_of(self, plan_id: str, limit: int = MAX_RESULTS) -> list[dict]:
        with self._lock:
            plan = self._find(plan_id)
            if plan is None:
                return []
            return [self._clone(r) for r in plan.get("results", [])[:max(0, int(limit))]]

    def set_next_run(self, plan_id: str, ts: float | None) -> None:
        with self._lock:
            plan = self._find(plan_id)
            if plan is None:
                return
            plan["next_run_at"] = ts
            self._save()

    # ---- 调度辅助 ----
    def due_ids(self, now: float | None = None) -> list[str]:
        now = self._clock() if now is None else now
        out = []
        with self._lock:
            for plan in self._data["plans"]:
                if not plan.get("enabled"):
                    continue
                nxt = plan.get("next_run_at")
                if nxt and nxt <= now:
                    out.append(plan["id"])
        return out

    def advance(self, plan_id: str, after_ts: float | None = None) -> None:
        """把下次执行推进到 after_ts 之后的第一个匹配时刻（跳过所有过期点）。"""
        after = self._clock() if after_ts is None else after_ts
        with self._lock:
            plan = self._find(plan_id)
            if plan is None:
                return
            try:
                plan["next_run_at"] = next_ts(plan["cron"], after)
            except PlanError:
                plan["next_run_at"] = None
            self._save()

    def reconcile(self, now: float | None = None) -> int:
        """启动时调用：过期计划一律推进到未来，实现「服务离线期间不补跑」。"""
        now = self._clock() if now is None else now
        changed = 0
        with self._lock:
            for plan in self._data["plans"]:
                nxt = plan.get("next_run_at")
                if plan.get("enabled") and (not nxt or nxt <= now):
                    try:
                        plan["next_run_at"] = next_ts(plan["cron"], now)
                    except PlanError:
                        plan["next_run_at"] = None
                    changed += 1
            if changed:
                self._save()
        return changed


# ---------------------------------------------------------------- 运行时调度


class PlanRuntime:
    """cron 触发 + 手动「立即执行」共用的运行时；executor(plan) -> (ok, message)。"""

    def __init__(self, store: PlanStore, executor, clock=time.time, tick_seconds: int = 30):
        self.store = store
        self._executor = executor
        self._clock = clock
        self._tick_seconds = max(int(tick_seconds), 1)
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def is_running(self, plan_id: str) -> bool:
        with self._lock:
            return plan_id in self._running

    def run_plan(self, plan_id: str, trigger: str = "manual") -> tuple[bool, str]:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            return False, "计划不存在"
        with self._lock:
            if plan_id in self._running:
                # 上一轮仍在运行或排队：跳过本次触发，不留堆积
                self.store.record_result(plan_id, "skipped", "上一轮仍在运行，跳过本次触发",
                                         0, trigger)
                return False, "上一轮仍在运行或排队，已跳过本次触发"
            self._running.add(plan_id)
        started = self._clock()
        try:
            ok, message = self._executor(plan)
        except Exception as exc:  # executor 自身异常也不能让调度线程崩
            ok, message = False, "执行异常: %s" % exc
        finally:
            with self._lock:
                self._running.discard(plan_id)
        duration_ms = int(max(0.0, self._clock() - started) * 1000)
        self.store.record_result(plan_id, "success" if ok else "failed",
                                 str(message or "")[:MAX_DESC_LEN], duration_ms, trigger)
        return bool(ok), str(message or "")

    def tick(self, now: float | None = None) -> list[str]:
        """处理所有到点计划，返回本次真正触发（非跳过）的 id 列表。"""
        now = self._clock() if now is None else now
        triggered = []
        for plan_id in self.store.due_ids(now):
            with self._lock:
                if plan_id in self._running:
                    # 无论 cron 还是手动，正在运行的一律跳过并推进下次执行
                    self.store.record_result(plan_id, "skipped",
                                             "上一轮仍在运行，跳过本次触发", 0, "cron")
                    self.store.advance(plan_id, now)
                    continue
            self.store.advance(plan_id, now)
            ok, _ = self.run_plan(plan_id, trigger="cron")
            triggered.append(plan_id)
        return triggered

    # ---- 后台线程 ----
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # 启动时把离线期间遗漏的触发点全部跳过
        self.store.reconcile(self._clock())
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="plugin-plan-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.wait(self._tick_seconds):
            try:
                self.tick()
            except Exception:
                continue


# ---------------------------------------------------------------- 进程单例

_store: PlanStore | None = None
_runtime: PlanRuntime | None = None


def get_plan_store() -> PlanStore:
    global _store
    if _store is None:
        _store = PlanStore()
    return _store


def get_plan_runtime() -> PlanRuntime | None:
    return _runtime


def start_plugin_plan_scheduler() -> PlanRuntime:
    """应用启动时调用（幂等）；只对已加载且已启用的插件触发 on_schedule。"""
    global _runtime
    if _runtime is not None and _runtime._thread is not None and _runtime._thread.is_alive():
        return _runtime

    def executor(plan: dict) -> tuple[bool, str]:
        # 延迟导入：本模块顶层保持零应用依赖
        from app.plugins import _registry

        pid = plan.get("plugin_id", "")
        plugin = _registry.get(pid)
        if plugin is None:
            return False, "插件未加载（外部插件需先启用）"
        if not plugin.enabled:
            return False, "插件未启用"
        try:
            plugin.on_schedule()
            return True, "运行成功"
        except Exception as exc:
            try:
                plugin.log("定时计划执行失败: %s" % exc, level="error", notify=False)
            except Exception:
                pass
            return False, "运行失败: %s" % exc

    _runtime = PlanRuntime(get_plan_store(), executor)
    _runtime.start()
    return _runtime


def reset_singletons() -> None:
    """仅供测试使用：清空进程单例，避免临时目录与运行时互相污染。"""
    global _store, _runtime
    if _runtime is not None:
        try:
            _runtime.stop()
        except Exception:
            pass
    _store = None
    _runtime = None
