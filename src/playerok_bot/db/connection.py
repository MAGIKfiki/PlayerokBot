"""З'єднання з SQLite та автоматичне застосування міграцій.

Єдина відповідальність цього модуля — низькорівневий доступ до файлу БД.
Жоден інший модуль бота не працює з SQLite напряму: усі звертаються через
репозиторії (db/orders.py, db/products.py тощо), які використовують connect()
звідси.

Жодних ручних дій після git pull: ensure_schema() викликається один раз при
старті бота (main.py) і сама доводить схему до останньої версії.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playerok_bot.db.migrations import MIGRATIONS


def _connect_raw(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    # foreign_keys — це pragma рівня з'єднання, не файлу: вмикати щоразу.
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL: один writer + кілька readers без взаємних блокувань — важливо,
    # якщо колись запрацює кілька процесів над одним файлом БД.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Короткоживуче з'єднання: комітить при успіху, завжди закривається.

    Стандартний безпечний патерн для SQLite — не тримати з'єднання довго
    відкритим між await-точками.
    """
    conn = _connect_raw(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def ensure_schema(db_path: Path) -> None:
    """Застосовує всі ще не застосовані міграції. Ідемпотентно, автоматично.

    Викликається один раз при старті бота — жодних ручних команд не потрібно.
    """
    with connect(db_path) as conn:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        for version, sql in MIGRATIONS:
            if version > current_version:
                conn.executescript(sql)
                # PRAGMA не підтримує bound-параметри; version — довірене
                # ціле число з нашого ж списку MIGRATIONS, не зовнішній ввід.
                conn.execute(f"PRAGMA user_version = {version}")
