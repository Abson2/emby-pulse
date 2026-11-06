from app.infra.db.schema_bootstrap import ensure_registered_table
from app.infra.db.system_store import system_store
from app.infra.db.ddl_bootstrap import ensure_registered_tables


def ensure_request_admin_messages_table() -> None:
    ensure_registered_tables(system_store, ensure_registered_table, ("request_admin_messages",))


def save_request_admin_message(tmdb_id, chat_id, message_id, is_caption, original_text) -> None:
    system_store.execute(
        """
        INSERT OR REPLACE INTO request_admin_messages
        (tmdb_id, chat_id, message_id, is_caption, original_text, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (int(tmdb_id), str(chat_id), int(message_id), 1 if is_caption else 0, original_text or ""),
    )


def list_request_admin_messages(tmdb_id: int):
    return system_store.fetch_all(
        """
        SELECT chat_id, message_id, is_caption, original_text
        FROM request_admin_messages
        WHERE tmdb_id = ?
        """,
        (int(tmdb_id),),
    )


def delete_request_admin_messages(tmdb_id: int) -> None:
    system_store.execute("DELETE FROM request_admin_messages WHERE tmdb_id = ?", (int(tmdb_id),))
