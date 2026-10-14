"""Персистентність надісланих сповіщень — аудит-трейл усіх спроб, не лише успішних."""

from __future__ import annotations

from pathlib import Path

from playerok_bot.db.accounts import DEFAULT_ACCOUNT_ID
from playerok_bot.db.connection import connect


def save_notification(
    db_path: Path,
    *,
    channel: str,
    notification_type: str,
    message_text: str,
    success: bool,
    deal_id: str | None = None,
    error_detail: str | None = None,
    account_id: int = DEFAULT_ACCOUNT_ID,
) -> None:
    with connect(db_path) as conn:
        order_id = None
        if deal_id is not None:
            row = conn.execute("SELECT id FROM orders WHERE deal_id = ?", (deal_id,)).fetchone()
            order_id = row["id"] if row is not None else None

        conn.execute(
            """
            INSERT INTO notifications (
                account_id, channel, order_id, notification_type,
                message_text, success, error_detail
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                channel,
                order_id,
                notification_type,
                message_text,
                int(success),
                error_detail,
            ),
        )


def has_notification_been_sent(db_path: Path, *, deal_id: str, notification_type: str) -> bool:
    """Чи вже було УСПІШНО надіслано сповіщення цього типу для цього
    замовлення — щоб не дублювати одноразові сповіщення (напр.
    "потрібна ручна передача товару") при кожній повторній перевірці."""
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM notifications n
            JOIN orders o ON o.id = n.order_id
            WHERE o.deal_id = ? AND n.notification_type = ? AND n.success = 1
            LIMIT 1
            """,
            (deal_id, notification_type),
        ).fetchone()
        return row is not None
