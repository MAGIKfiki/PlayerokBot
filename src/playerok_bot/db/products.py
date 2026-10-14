"""Персистентність товарів продавця.

Єдина відповідальність — get-or-create/update запису products за природним
ключем (account_id, product_url). Інші модулі не працюють з SQLite напряму.
"""

from __future__ import annotations

from pathlib import Path

from playerok_bot.db.accounts import DEFAULT_ACCOUNT_ID
from playerok_bot.db.connection import connect


def get_or_create_product(
    db_path: Path, product_url: str, name: str, *, account_id: int = DEFAULT_ACCOUNT_ID
) -> int:
    """Повертає id товару: створює запис при першій появі, інакше оновлює name/last_seen_at."""
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO products (account_id, product_url, name)
            VALUES (?, ?, ?)
            ON CONFLICT (account_id, product_url) DO UPDATE SET
                name = excluded.name,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (account_id, product_url, name),
        )
        row = conn.execute(
            "SELECT id FROM products WHERE account_id = ? AND product_url = ?",
            (account_id, product_url),
        ).fetchone()
        return int(row["id"])
