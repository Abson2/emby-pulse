import os
import sqlite3
from contextlib import contextmanager

from app.core.config import SYSTEM_DB_PATH

from .row import to_data_row


class SystemStore:
    """Explicit access boundary for the EmbyPulse system database."""

    def __init__(self, db_path: str = SYSTEM_DB_PATH):
        self.db_path = db_path

    @contextmanager
    def connect(self, timeout: float = 20.0):
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def fetch_all(self, sql: str, params=()):
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            return [to_data_row(row) for row in cur.fetchall()]

    def fetch_one(self, sql: str, params=()):
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            return to_data_row(cur.fetchone())

    def execute(self, sql: str, params=()) -> int:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount

    def execute_many(self, statements) -> int:
        """2.0 批次 B4：多条写语句共用一次连接与一次事务（纯加法）。

        `statements` 是 ``(sql, params)`` 序列；返回最后一条语句的 rowcount。
        任一句失败则整批回滚并抛出，避免留下半批数据。

        该方法**不替换** ``execute()``：既有调用点每条语句独立提交的语义
        完全不变，只有新增的批量场景才显式使用它，保证 100% 行为等价。
        """
        rowcount = 0
        with self.connect() as conn:
            cur = conn.cursor()
            try:
                for sql, params in statements:
                    cur.execute(sql, params)
                    rowcount = cur.rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return rowcount


system_store = SystemStore()
