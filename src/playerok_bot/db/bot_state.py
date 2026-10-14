"""Поточний операційний стан бота — окремо від settings (це конфігурація).

Дозволяє після рестарту зрозуміти: чи бот працював, коли востаннє був
"живий" (heartbeat), коли востаннє логінився, яку помилку бачив останньою.
"""

from __future__ import annotations

from pathlib import Path

from playerok_bot.db.accounts import DEFAULT_ACCOUNT_ID
from playerok_bot.db.connection import connect


def _upsert(db_path: Path, account_id: int, set_clause: str, params: tuple[object, ...]) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO bot_state (account_id) VALUES (?) ON CONFLICT (account_id) DO NOTHING",
            (account_id,),
        )
        conn.execute(
            f"UPDATE bot_state SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE account_id = ?",
            (*params, account_id),
        )


def mark_running(db_path: Path, is_running: bool, *, account_id: int = DEFAULT_ACCOUNT_ID) -> None:
    _upsert(db_path, account_id, "is_running = ?", (int(is_running),))


def mark_heartbeat(db_path: Path, *, account_id: int = DEFAULT_ACCOUNT_ID) -> None:
    _upsert(db_path, account_id, "last_heartbeat_at = CURRENT_TIMESTAMP", ())


def mark_successful_cycle(db_path: Path, *, account_id: int = DEFAULT_ACCOUNT_ID) -> None:
    _upsert(db_path, account_id, "last_successful_cycle_at = CURRENT_TIMESTAMP", ())


def mark_backlog_scan(db_path: Path, *, account_id: int = DEFAULT_ACCOUNT_ID) -> None:
    """Окремо від last_successful_cycle_at (той — для швидкого order-polling
    циклу) — час останнього успішного проходу незалежного backlog-скану."""
    _upsert(db_path, account_id, "last_backlog_scan_at = CURRENT_TIMESTAMP", ())


def mark_login(db_path: Path, *, account_id: int = DEFAULT_ACCOUNT_ID) -> None:
    _upsert(db_path, account_id, "last_login_at = CURRENT_TIMESTAMP", ())


def mark_error(db_path: Path, error_text: str, *, account_id: int = DEFAULT_ACCOUNT_ID) -> None:
    _upsert(db_path, account_id, "last_error = ?, last_error_at = CURRENT_TIMESTAMP", (error_text,))
