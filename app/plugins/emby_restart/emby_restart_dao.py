from app.infra.db.schema_bootstrap import ensure_registered_table
from app.infra.db.system_store import system_store
from app.infra.db.ddl_bootstrap import ensure_registered_tables


def ensure_emby_restart_history_table() -> None:
    ensure_registered_tables(system_store, ensure_registered_table, ("emby_restart_history",))


def list_emby_restart_history(limit: int = 20):
    ensure_emby_restart_history_table()
    return system_store.fetch_all(
        """
        SELECT time, mode, success, detail
        FROM emby_restart_history
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )


def create_emby_restart_history(record: dict) -> None:
    ensure_emby_restart_history_table()
    system_store.execute(
        """
        INSERT INTO emby_restart_history (time, mode, success, detail)
        VALUES (?, ?, ?, ?)
        """,
        (
            record["time"],
            record.get("mode", ""),
            1 if record["success"] else 0,
            record.get("detail", ""),
        ),
    )
