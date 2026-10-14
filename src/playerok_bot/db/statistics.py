"""Матеріалізований кеш статистики.

Ніколи не джерело істини: recompute_statistics можна викликати повторно
будь-коли для будь-якої дати, і результат завжди буде тим самим, що
обчислений напряму з orders/relist_attempts/notifications.
"""

from __future__ import annotations

from pathlib import Path

from playerok_bot.db.accounts import DEFAULT_ACCOUNT_ID
from playerok_bot.db.connection import connect


def recompute_statistics(
    db_path: Path, date: str, *, account_id: int = DEFAULT_ACCOUNT_ID, currency: str = "RUB"
) -> None:
    """Перераховує рядок statistics за (account_id, date, currency) з сирих таблиць."""
    with connect(db_path) as conn:
        orders_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_value), 0) AS amount_sum
            FROM orders
            WHERE account_id = ? AND currency = ? AND date(discovered_at) = ?
            """,
            (account_id, currency, date),
        ).fetchone()

        notif_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN success THEN 1 ELSE 0 END), 0) AS sent_cnt,
                COALESCE(SUM(CASE WHEN success THEN 0 ELSE 1 END), 0) AS failed_cnt
            FROM notifications
            WHERE account_id = ? AND date(sent_at) = ?
            """,
            (account_id, date),
        ).fetchone()

        relist_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN ra.success THEN 1 ELSE 0 END), 0) AS success_cnt,
                COALESCE(SUM(CASE WHEN ra.success THEN 0 ELSE 1 END), 0) AS failed_cnt,
                COALESCE(SUM(ra.cost_amount), 0) AS cost_sum
            FROM relist_attempts ra
            JOIN orders o ON o.id = ra.order_id
            WHERE o.account_id = ? AND ra.currency = ? AND date(ra.attempted_at) = ?
            """,
            (account_id, currency, date),
        ).fetchone()

        conn.execute(
            """
            INSERT INTO statistics (
                account_id, date, currency, orders_count, orders_amount_sum,
                notifications_sent_count, notifications_failed_count,
                relist_success_count, relist_failed_count, relist_cost_sum, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (account_id, date, currency) DO UPDATE SET
                orders_count = excluded.orders_count,
                orders_amount_sum = excluded.orders_amount_sum,
                notifications_sent_count = excluded.notifications_sent_count,
                notifications_failed_count = excluded.notifications_failed_count,
                relist_success_count = excluded.relist_success_count,
                relist_failed_count = excluded.relist_failed_count,
                relist_cost_sum = excluded.relist_cost_sum,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                account_id,
                date,
                currency,
                orders_row["cnt"],
                orders_row["amount_sum"],
                notif_row["sent_cnt"],
                notif_row["failed_cnt"],
                relist_row["success_cnt"],
                relist_row["failed_cnt"],
                relist_row["cost_sum"],
            ),
        )
