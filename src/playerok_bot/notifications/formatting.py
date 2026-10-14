"""Форматування тексту сповіщень зі структурованих даних Playerok.

Окремо від каналів сповіщень (Telegram, Discord тощо) — ті лише доставляють
вже готовий текст і нічого не знають про Playerok чи Order.
"""

from __future__ import annotations

from playerok_bot.db.relist_attempts import AttemptLogEntry
from playerok_bot.playerok.orders import Order


def format_new_order_message(order: Order) -> str:
    """Формує текст сповіщення про нове знайдене замовлення."""
    return (
        "🛒 Нове замовлення!\n\n"
        f"📦 Товар: {order.product_name}\n"
        f"💰 Сума: {order.amount}\n"
        f"👤 Покупець: {order.buyer_name}\n"
        f"📌 Статус: {order.status}\n\n"
        "💬 Чат:\n"
        f"{order.conversation_url}"
    )


def format_relist_success_message(order: Order) -> str:
    """Товар успішно перевиставлено (Premium) — одне з трьох "важливих"
    сповіщень, на які підписаний користувач (2026-07-21)."""
    return (
        "✅ Товар перевиставлено (Премиум)\n\n"
        f"📦 Товар: {order.product_name}\n"
        f"🔗 {order.product_url}"
    )


def format_awaiting_delivery_message(order: Order) -> str:
    """Угода ще в статусі "Выполнение" — товар фізично не передано покупцю,
    потрібна ручна дія продавця в Roblox. Надсилається РІВНО ОДИН РАЗ на
    замовлення (db_notifications.has_notification_been_sent) — подальші
    періодичні перевірки статусу угоди (playerok/deal_status.py) мовчазні,
    щоб не спамити після кожної перевірки."""
    return (
        "📦 Потрібна ручна передача товару\n\n"
        f"Товар: {order.product_name}\n"
        f"Покупець: {order.buyer_name}\n\n"
        "Угода ще в статусі «Выполнение» — Playerok не дозволить "
        "автоматичний Premium-реліст, поки товар не передано покупцю в "
        "Roblox і не натиснута «Я выполнил» на сторінці угоди.\n\n"
        f"💬 Чат:\n{order.conversation_url}"
    )


def format_relist_exhausted_message(
    order: Order, history: tuple[AttemptLogEntry, ...]
) -> str:
    """Звіт про товар, чиї автоматичні спроби перевиставлення вичерпано
    (db/relist_attempts.py::record_relist_outcome, status='relist_failed') —
    одне повідомлення з ПОВНОЮ історією всіх спроб, а не лише останньої."""
    lines = [
        "⚠️ Не вдалося перевиставити товар після продажу (вичерпано спроби)",
        "",
        f"📦 Товар: {order.product_name}",
        f"🔗 {order.product_url}",
        "",
        f"Спроб: {len(history)}",
    ]
    for entry in history:
        lines.append(f"  {entry.attempt_number}. [{entry.attempted_at}] {entry.reason}")
    return "\n".join(lines)
