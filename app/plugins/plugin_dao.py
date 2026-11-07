import json
from datetime import datetime, timedelta

from app.infra.db.schema_bootstrap import ensure_registered_table
from app.infra.db.system_store import system_store

_PLUGIN_REGISTRY_TABLES = (
    "plugin_state",
    "plugin_logs",
)


def ensure_plugin_tables() -> None:
    with system_store.connect(timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()
        for table_name in _PLUGIN_REGISTRY_TABLES:
            ensure_registered_table(cursor, table_name)
        conn.commit()


def list_plugin_states():
    return system_store.fetch_all("SELECT plugin_id, enabled, config FROM plugin_state")


def set_plugin_enabled(plugin_id: str, enabled: bool) -> None:
    system_store.execute(
        """
        INSERT OR REPLACE INTO plugin_state (plugin_id, enabled, config)
        VALUES (?, ?, COALESCE((SELECT config FROM plugin_state WHERE plugin_id = ?), '{}'))
        """,
        (plugin_id, 1 if enabled else 0, plugin_id),
    )


def delete_plugin_state(plugin_id: str) -> None:
    """2.3：卸载外部插件时清理启用状态，避免重新导入同 id 被自动启用。"""
    system_store.execute("DELETE FROM plugin_state WHERE plugin_id = ?", (plugin_id,))


def get_plugin_config(plugin_id: str) -> dict:
    row = system_store.fetch_one("SELECT config FROM plugin_state WHERE plugin_id = ?", (plugin_id,))
    try:
        return json.loads(row["config"]) if row and row["config"] else {}
    except Exception:
        return {}


def save_plugin_config(plugin_id: str, config: dict) -> None:
    system_store.execute(
        """
        INSERT OR REPLACE INTO plugin_state (plugin_id, enabled, config)
        VALUES (?, COALESCE((SELECT enabled FROM plugin_state WHERE plugin_id = ?), 0), ?)
        """,
        (plugin_id, plugin_id, json.dumps(config, ensure_ascii=False)),
    )


# 插件日志管控上限（大小 + 时间），避免日志表无限增长 / 单条过长
PLUGIN_LOG_MAX_ROWS = 500            # 单插件日志行数上限（大小管控）
PLUGIN_LOG_RETENTION_DAYS = 30       # 日志留存天数上限（时间管控）
PLUGIN_LOG_MESSAGE_MAX = 2000        # 单条日志消息长度上限（避免冗长）


def add_plugin_log(plugin_id: str, level: str, message: str) -> None:
    # 单条消息截断，避免把整段渲染体等超长内容写进日志
    message = (message or "")[:PLUGIN_LOG_MESSAGE_MAX]
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    retention_before = (datetime.now() - timedelta(days=PLUGIN_LOG_RETENTION_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    # 2.0 性能：三条语句合并为「单次连接 + 单事务」，避免 3 次 sqlite3.connect/commit。
    # 对外行为不变：函数仍返回 None，表最终状态与原先逐条 execute 完全一致。
    with system_store.connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO plugin_logs (plugin_id, level, message, created_at) VALUES (?, ?, ?, ?)",
            (plugin_id, level, message, created_at),
        )
        # 大小上限：保留最近 N 条，删除更旧的。
        # 等价于原 `id NOT IN (SELECT id ... ORDER BY id DESC LIMIT ?)`：
        # 由于 id 单调递增，被删集合恰为 id < 最近 N 条中最小的那个 id，
        # 改为主键范围删除后不再对全表做 NOT IN 比对。
        cursor.execute(
            "DELETE FROM plugin_logs WHERE plugin_id = ?"
            " AND id < ("
            "   SELECT MIN(id) FROM ("
            "     SELECT id FROM plugin_logs WHERE plugin_id = ?"
            "     ORDER BY id DESC LIMIT ?"
            "   )"
            " )",
            (plugin_id, plugin_id, PLUGIN_LOG_MAX_ROWS),
        )
        # 时间上限：删除超过留存天数的旧日志（SQL 与原实现逐字一致）
        cursor.execute(
            "DELETE FROM plugin_logs WHERE plugin_id = ? AND created_at < ?",
            (plugin_id, retention_before),
        )
        conn.commit()


def list_plugin_logs(plugin_id: str, limit: int = 50):
    return system_store.fetch_all(
        """
        SELECT level, message, created_at
        FROM plugin_logs
        WHERE plugin_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (plugin_id, limit),
    )


def clear_plugin_logs(plugin_id: str) -> None:
    system_store.execute("DELETE FROM plugin_logs WHERE plugin_id = ?", (plugin_id,))
