"""Прості налаштування типу ключ-значення (таблиця settings).

На відміну від bot_state (поточний операційний стан), тут — конфігурація,
яку можна міняти без рестарту бота: кожен фоновий цикл сам перечитує потрібні
йому значення на початку ітерації.
"""

from __future__ import annotations

from pathlib import Path

from playerok_bot.db.connection import connect


def get_setting(db_path: Path, key: str, default: str) -> str:
    with connect(db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else default


def set_setting(db_path: Path, key: str, value: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )


def get_int_setting(db_path: Path, key: str, default: int) -> int:
    raw = get_setting(db_path, key, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def get_bool_setting(db_path: Path, key: str, default: bool) -> bool:
    raw = get_setting(db_path, key, str(default)).strip().lower()
    return raw in ("1", "true", "yes", "on")
