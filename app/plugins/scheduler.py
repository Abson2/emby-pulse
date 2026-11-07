# -*- coding: utf-8 -*-
"""插件中心「定时执行」全局调度器（2.5）。

设计约束：
* 纯标准库、不依赖 fastapi / 数据库 —— 便于纯标准库契约测试直接 import；
* 间隔与开关存放在各插件**自身配置**的 ``schedule_minutes`` 键（0 = 关），
  复用既有 plugin_dao 配置读写，不新增表、不改对外接口；
* 只对「已加载」的插件实例触发 ``on_schedule()`` 钩子 —— 未启用的外部插件
  代码从未 import，绝不因调度而被执行（延续 2.3 的安全边界）；
* 手动「立即执行」与到点触发共用同一个 run_hook，由调用方注入，本模块
  不感知 PluginBase / 路由，保证可独立测试。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Iterable, List, Optional

# 单次 tick 的轮询间隔（秒）；也是手动刷新 last_run 的最小精度
TICK_SECONDS = 60
# schedule_minutes 允许的上限（7 天），防止误填超大值
MAX_MINUTES = 7 * 24 * 60


class PluginScheduler:
    """按分钟间隔触发插件 ``on_schedule`` 钩子的极简调度器。

    Args:
        entries_provider: 返回 ``[{"id": str, "minutes": int, "runnable": bool}, ...]``
            的可调用对象；每次 tick 重新取值，便于配置热更新。
        run_hook: ``run_hook(plugin_id)`` 执行插件动作；异常由其内部消化。
        clock: 时间函数（默认 time.time），测试可注入假时钟。
        tick_seconds: 后台线程轮询间隔。
    """

    def __init__(
        self,
        entries_provider: Callable[[], Iterable[dict]],
        run_hook: Callable[[str], None],
        clock: Callable[[], float] = time.time,
        tick_seconds: int = TICK_SECONDS,
    ):
        self._entries_provider = entries_provider
        self._run_hook = run_hook
        self._clock = clock
        self._tick_seconds = max(int(tick_seconds), 1)
        self._last_run: Dict[str, float] = {}
        self._started_at: float = clock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ---- 核心调度逻辑（纯函数式，可独立测试） ----

    def due_ids(self, now: Optional[float] = None) -> List[str]:
        """返回本次应当触发的插件 id 列表。"""
        now = self._clock() if now is None else now
        due: List[str] = []
        for entry in self._entries_provider():
            try:
                minutes = int(entry.get("minutes") or 0)
            except (TypeError, ValueError):
                continue
            pid = entry.get("id") or ""
            if not pid or minutes <= 0 or not entry.get("runnable"):
                continue
            minutes = min(minutes, MAX_MINUTES)
            with self._lock:
                last = self._last_run.get(pid, self._started_at)
            if now - last >= minutes * 60:
                due.append(pid)
        return due

    def tick(self, now: Optional[float] = None) -> List[str]:
        """执行所有到点插件并记录 last_run；返回实际触发的 id 列表。"""
        now = self._clock() if now is None else now
        ran: List[str] = []
        for pid in self.due_ids(now):
            try:
                self._run_hook(pid)
            except Exception:
                # run_hook 理论上自带异常隔离；这里兜底保证一个插件失败不影响其他
                continue
            with self._lock:
                self._last_run[pid] = now
            ran.append(pid)
        return ran

    def mark_ran(self, plugin_id: str, now: Optional[float] = None) -> None:
        """手动执行后同步 last_run，避免手动触发后立即又被到点触发。"""
        with self._lock:
            self._last_run[plugin_id] = self._clock() if now is None else now

    def last_run_of(self, plugin_id: str) -> Optional[float]:
        with self._lock:
            value = self._last_run.get(plugin_id)
        return value

    def next_due_in(self, plugin_id: str, minutes: int, now: Optional[float] = None) -> Optional[int]:
        """距下次到点的秒数；未配置间隔返回 None。"""
        if minutes <= 0:
            return None
        now = self._clock() if now is None else now
        with self._lock:
            last = self._last_run.get(plugin_id, self._started_at)
        remain = int(last + minutes * 60 - now)
        return max(remain, 0)

    # ---- 后台线程 ----

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="plugin-scheduler", daemon=True
        )
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
                # 调度器自身绝不能把应用线程带崩
                continue


# ---- 进程级单例（由 bootstrap 启动，路由层读取） ----

_scheduler: Optional[PluginScheduler] = None


def get_plugin_scheduler() -> Optional[PluginScheduler]:
    return _scheduler


def start_plugin_scheduler() -> None:
    """启动全局插件调度器（幂等）；应用启动时调用一次。"""
    global _scheduler
    if _scheduler is not None and _scheduler._thread is not None and _scheduler._thread.is_alive():
        return

    def entries_provider() -> Iterable[dict]:
        # 延迟导入：本模块保持零应用依赖
        from app.plugins import _registry

        out = []
        for pid, plugin in list(_registry.items()):
            try:
                cfg = plugin._get_config()
                minutes = int(cfg.get("schedule_minutes") or 0)
            except Exception:
                minutes = 0
            out.append({"id": pid, "minutes": minutes, "runnable": True})
        return out

    def run_hook(pid: str) -> None:
        from app.plugins import _registry

        plugin = _registry.get(pid)
        if plugin is None or not plugin.enabled:
            return
        try:
            plugin.on_schedule()
        except Exception as exc:
            try:
                plugin.log("定时执行失败: %s" % exc, level="error", notify=False)
            except Exception:
                pass

    _scheduler = PluginScheduler(entries_provider, run_hook)
    _scheduler.start()
