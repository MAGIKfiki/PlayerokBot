"""Журнал системних подій.

Свідомо вузька мета — лише значущі системні події (закритий список, той
самий, що й CHECK-обмеження в схемі), а НЕ дзеркало кожного logger.info().
Доменні події (нове замовлення, перевиставлення) вже structured-записані в
orders/relist_attempts/notifications — тут їх дублювати не потрібно.
"""

from __future__ import annotations

import json
from pathlib import Path

from playerok_bot.db.accounts import DEFAULT_ACCOUNT_ID
from playerok_bot.db.connection import connect

EVENT_TYPES = (
    "startup",
    "shutdown",
    "critical_error",
    "unhandled_exception",
    "network_loss",
    "playwright_error",
    "auth_failure",
    "crash",
)


def log_event(
    db_path: Path,
    event_type: str,
    message: str,
    *,
    account_id: int | None = DEFAULT_ACCOUNT_ID,
    details: dict[str, object] | None = None,
) -> None:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Невідомий event_type: {event_type!r} (дозволено: {EVENT_TYPES})")
    details_json = json.dumps(details, ensure_ascii=False) if details is not None else None
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO logs (event_type, account_id, message, details_json) VALUES (?, ?, ?, ?)",
            (event_type, account_id, message, details_json),
        )
