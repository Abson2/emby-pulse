"""DDL / bootstrap 一次性执行的通用缓存（2.0 批次 B3）。

背景：全库约 40 个 ``ensure_*_tables()`` 各自重复「加锁 → 查缓存 → 执行 DDL
→ 标记」的样板，且部分在请求路径里每次执行。本模块把该模式收敛为可复用
的 :class:`EnsureOnce`，供逐个迁移使用。

等价性契约：
- **按 key 缓存**：默认 key 为 ``system_store.db_path``，换库自动失效；
- **异常不写缓存**：``fn()`` 抛异常时直接向外抛出，不标记完成，下次重试；
- **reset 可控**：显式清空后重新执行，供测试 / 迁移 / 外部重建数据库使用；
- **锁内执行**：与既有实现一致，避免并发请求重复跑 DDL；
- 支持传入外部 ``lock`` / ``ready`` 字典，便于迁移时保留模块原有变量名，
  不改动任何对外可见符号。

注意：带「运行时动态加列 / 数据迁移」语义的 ``ensure_*`` 不能盲目套用，
需先分类为「纯建表」与「带迁移」，只对前者接入；本模块不自动替任何函数加缓存。
"""
from __future__ import annotations

import threading


class EnsureOnce:
    """同一个 key 下只成功执行一次 ``fn`` 的线程安全门控。"""

    def __init__(self, lock=None, ready=None):
        self._lock = lock if lock is not None else threading.Lock()
        self._ready = ready if ready is not None else {}

    def reset(self) -> None:
        """清空完成标记（不清空正在执行的调用）。"""
        with self._lock:
            self._ready.clear()

    def run(self, key, fn) -> bool:
        """执行 ``fn`` 并标记完成；返回本次是否真正执行。

        ``fn`` 抛异常时不写缓存，异常原样向外传播，下次调用会重试。
        """
        with self._lock:
            if self._ready.get(key):
                return False
            fn()
            self._ready[key] = True
            return True

    def is_ready(self, key) -> bool:
        """只读查询（测试 / 诊断用），不改变状态。"""
        with self._lock:
            return bool(self._ready.get(key))
