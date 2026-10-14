"""Персистентність замовлень (продажів) — orders.py (Playerok-читання) не
знає про SQLite напряму, це роблять функції тут.

deal_id — головний природний ключ угоди (глобально унікальний UUID
Playerok), тому дедуплікація сповіщень і перевиставлення прив'язана саме до
нього, а не до product_url: той самий товар, проданий двічі, отримує дві
незалежні спроби перевиставлення (по одній на кожен deal_id).
"""

from __future__ import annotations

from pathlib import Path

from playerok_bot.db.accounts import DEFAULT_ACCOUNT_ID
from playerok_bot.db.connection import connect
from playerok_bot.db.products import get_or_create_product
from playerok_bot.playerok.orders import PLAYEROK_BASE_URL, Order


def save_order(db_path: Path, order: Order, *, account_id: int = DEFAULT_ACCOUNT_ID) -> int:
    """Ідемпотентно зберігає замовлення (за deal_id) і повертає id рядка orders.

    Повторний виклик з тим самим deal_id нічого не змінює (ON CONFLICT DO
    NOTHING) — новий поліл того самого замовлення не створює дублікат.
    """
    product_id = get_or_create_product(
        db_path, order.product_url, order.product_name, account_id=account_id
    )
    amount_value = _parse_amount(order.amount)
    deal_url = f"{PLAYEROK_BASE_URL}/deal/{order.deal_id}"

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO orders (
                account_id, deal_id, deal_url, product_id, product_name_snapshot,
                amount_raw, amount_value, buyer_name, conversation_url, status_raw
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (deal_id) DO NOTHING
            """,
            (
                account_id,
                order.deal_id,
                deal_url,
                product_id,
                order.product_name,
                order.amount,
                amount_value,
                order.buyer_name,
                order.conversation_url,
                order.status,
            ),
        )
        row = conn.execute("SELECT id FROM orders WHERE deal_id = ?", (order.deal_id,)).fetchone()
        return int(row["id"])


def deal_exists(db_path: Path, deal_id: str) -> bool:
    """Чи вже відомий бот про цю угоду (за deal_id) — незалежно від статусу.

    Використовується backlog-виявленням (playerok/backlog_scan.py): якщо
    угода вже відома, її подальшою обробкою опікується звичайний
    order-polling цикл (get_unfinished_orders) — не потрібно заново
    navigate на сторінку угоди щосканування лише щоб це перевірити.
    """
    with connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM orders WHERE deal_id = ?", (deal_id,)).fetchone()
        return row is not None


def has_order_been_notified(db_path: Path, deal_id: str) -> bool:
    """Замінює in-memory notified_conversation_urls — переживає рестарт бота."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT notified_at FROM orders WHERE deal_id = ?", (deal_id,)
        ).fetchone()
        return row is not None and row["notified_at"] is not None


def mark_order_notified(db_path: Path, deal_id: str) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE orders
            SET bot_status = 'notified',
                notified_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE deal_id = ? AND notified_at IS NULL
            """,
            (deal_id,),
        )


def needs_relist_attempt_now(db_path: Path, deal_id: str) -> bool:
    """Чи варто прямо зараз викликати relist_product() для цього замовлення.

    Замінює колишню бінарну has_relist_been_attempted(): тепер є проміжний
    стан 'pending_relist' (тимчасова невдача, чекає запланованого retry за
    next_retry_at — db/relist_attempts.py), тому "вже намагались" ≠ "більше
    не намагатись". False, якщо: остаточно завершено (relisted/relist_failed),
    АБО retry заплановано на майбутнє (next_retry_at ще не настав).
    """
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT o.bot_status, ra.next_retry_at
            FROM orders o
            LEFT JOIN relist_attempts ra ON ra.order_id = o.id
            WHERE o.deal_id = ?
            """,
            (deal_id,),
        ).fetchone()
        if row is None:
            return False
        if row["bot_status"] in ("relisted", "relist_failed"):
            return False
        next_retry_at = row["next_retry_at"]
        if next_retry_at is None:
            return True
        due_row = conn.execute(
            "SELECT ? <= CURRENT_TIMESTAMP AS is_due", (next_retry_at,)
        ).fetchone()
        return bool(due_row["is_due"])


def get_unfinished_orders(db_path: Path) -> list[Order]:
    """Повертає замовлення, які бот ще не довів до кінця (не relisted/relist_failed)
    і чий retry (якщо заплановано) уже настав.

    Реконструює повний Order прямо зі збережених у БД полів — НЕ потребує
    повторного відкриття сторінки угоди на Playerok, яка могла вже зникнути
    зі списку "нових" (напр. статус угоди на маркетплейсі змінився). Це і є
    механізм, завдяки якому джерелом істини для "що ще треба зробити" є БД,
    а не живий список на сторінці Playerok.

    Використовується на початку кожного циклу опитування — щоб замовлення,
    перерване аварійним завершенням процесу (краш, вимкнення живлення тощо)
    між збереженням і завершенням обробки, гарантовано підхопилось знову, і
    щоб заплановані retry (pending_relist) підхоплювались автоматично, як
    тільки настає їхній час — включно одразу після рестарту бота.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT o.deal_id, o.product_name_snapshot, p.product_url,
                   o.amount_raw, o.buyer_name, o.conversation_url, o.status_raw
            FROM orders o
            JOIN products p ON p.id = o.product_id
            LEFT JOIN relist_attempts ra ON ra.order_id = o.id
            WHERE o.bot_status NOT IN ('relisted', 'relist_failed')
              AND (ra.next_retry_at IS NULL OR ra.next_retry_at <= CURRENT_TIMESTAMP)
            ORDER BY o.discovered_at ASC
            """
        ).fetchall()
        return [
            Order(
                deal_id=row["deal_id"],
                product_name=row["product_name_snapshot"],
                product_url=row["product_url"],
                amount=row["amount_raw"],
                buyer_name=row["buyer_name"],
                conversation_url=row["conversation_url"],
                status=row["status_raw"],
            )
            for row in rows
        ]


def _parse_amount(amount_raw: str) -> float | None:
    """Best-effort парсинг "90 ₽" -> 90.0. Ніколи не авторитетне значення —
    amount_raw лишається джерелом істини, це лише зручність для статистики."""
    digits = "".join(ch for ch in amount_raw if ch.isdigit() or ch in ".,")
    digits = digits.replace(",", ".")
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None
