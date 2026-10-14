"""ЄДИНИЙ relist-пайплайн для ОБОХ джерел замовлень: живого order-polling
(main.py, знаходить нові продажі в реальному часі) і backlog-виявлення
пропущених угод (playerok/backlog_scan.py, знаходить старі/пропущені
продажі за словником King Legacy).

Рішення 2026-07-21 (аудит надійності): раніше backlog-потік мав ОКРЕМУ,
спрощену копію логіки перевиставлення (без deal_status-перевірки, без
retry/backoff) — реальний ризик, що backlog тихо позначить товар
"не вдалося" назавжди після першої ж тимчасової невдачі (саме та помилка,
яку щойно виправили для order-потоку). Замість підтримувати дві копії — одна
реалізація тут, обидва виклики (main.py й backlog_scan.py) використовують
САМЕ ЇЇ. Джерело замовлення (щойно куплено чи знайдено заднім числом) не
впливає на те, як воно обробляється — обидва одразу стають звичайним рядком
у orders/relist_attempts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Page

from playerok_bot.db import notifications as db_notifications
from playerok_bot.db import orders as db_orders
from playerok_bot.db import relist_attempts as db_relist_attempts
from playerok_bot.db import statistics as db_statistics
from playerok_bot.notifications.formatting import (
    format_awaiting_delivery_message,
    format_new_order_message,
    format_relist_exhausted_message,
    format_relist_success_message,
)
from playerok_bot.notifications.telegram import TelegramNotifier
from playerok_bot.playerok import deal_status as deal_status_module
from playerok_bot.playerok.deal_status import DealStage
from playerok_bot.playerok.orders import Order
from playerok_bot.playerok.relist import RelistResult, relist_product

logger = logging.getLogger(__name__)


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def get_relist_lock(locks: dict[str, asyncio.Lock], product_url: str) -> asyncio.Lock:
    """Спільний лок за product_url між УСІМА викликачами relist_product()
    (order-loop, backlog-loop) — інакше теоретично можливий одночасний клік
    по тому самому товару з двох різних Page."""
    lock = locks.get(product_url)
    if lock is None:
        lock = asyncio.Lock()
        locks[product_url] = lock
    return lock


def get_deal_lock(locks: dict[str, asyncio.Lock], deal_id: str) -> asyncio.Lock:
    """Спільний лок за order.deal_id між УСІМА викликачами process_order()
    (order-loop, backlog-loop).

    Виявлено 2026-07-23 (перший production-тест backlog-перевиставлення):
    relist_locks (за product_url) захищає лише сам клік у relist_product(),
    а не весь process_order() — тож щойно backlog-скан зберігав нове
    замовлення (db_orders.save_order на самому початку process_order),
    воно одразу ставало видимим для db_orders.get_unfinished_orders(), і
    швидкий order-loop (цикл 2-5с) встигав підхопити ТЕ САМЕ замовлення й
    запустити СВІЙ власний process_order() ПАРАЛЕЛЬНО — поки перший виклик
    ще не встиг дописати результат. relist_locks не пускав два кліки
    одночасно (без ризику подвійної оплати), але кожен виклик незалежно
    записував власний relist_attempts outcome — реальне замовлення
    отримувало 2 витрачені спроби retry-бюджету замість однієї за одну
    й ту саму подію. Цей лок обгортає ВЕСЬ process_order(), тому другий
    викликач фізично не може увійти в тіло функції, поки перший не
    завершить її повністю (і встигне записати результат, який другий потім
    побачить як актуальний стан)."""
    lock = locks.get(deal_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[deal_id] = lock
    return lock


async def process_order(
    page: Page,
    notifier: TelegramNotifier,
    db_path: Path,
    order: Order,
    relist_locks: dict[str, asyncio.Lock],
    deal_locks: dict[str, asyncio.Lock],
) -> None:
    """Доводить одне замовлення до кінця: сповіщення + перевиставлення, кожне
    рівно один раз, ідемпотентно за order.deal_id.

    Єдина точка входу для БУДЬ-ЯКОГО джерела замовлення: щойно знайденого
    нового (main.py), "незавершеного" з get_unfinished_orders() після
    рестарту, або заднім числом знайденого через backlog-скан
    (playerok/backlog_scan.py) — усі йдуть сюди й обробляються однаково.

    Обгорнуто deal_locks (get_deal_lock) — один deal_id фізично не може
    перебувати всередині цієї функції з двох викликів одночасно, незалежно
    від того, який саме цикл (order-loop чи backlog-loop) його викликав.
    """
    async with get_deal_lock(deal_locks, order.deal_id):
        await _process_order_locked(page, notifier, db_path, order, relist_locks)


async def _process_order_locked(
    page: Page,
    notifier: TelegramNotifier,
    db_path: Path,
    order: Order,
    relist_locks: dict[str, asyncio.Lock],
) -> None:
    db_orders.save_order(db_path, order)

    if not db_orders.has_order_been_notified(db_path, order.deal_id):
        message = format_new_order_message(order)
        sent = await notifier.send(message)
        db_notifications.save_notification(
            db_path,
            channel="telegram",
            notification_type="new_order",
            message_text=message,
            success=sent,
            deal_id=order.deal_id,
        )
        if sent:
            db_orders.mark_order_notified(db_path, order.deal_id)
            logger.info("Надіслано сповіщення про замовлення: %s", order.deal_id)
        else:
            logger.warning(
                "Не вдалося надіслати сповіщення про замовлення %s — "
                "спробуємо ще раз наступного циклу",
                order.deal_id,
            )

    if db_orders.needs_relist_attempt_now(db_path, order.deal_id):
        await attempt_relist_with_deal_check(page, notifier, db_path, order, relist_locks)

    db_statistics.recompute_statistics(db_path, _today_utc())


async def attempt_relist_with_deal_check(
    page: Page,
    notifier: TelegramNotifier,
    db_path: Path,
    order: Order,
    relist_locks: dict[str, asyncio.Lock],
) -> None:
    """Спершу перевіряє РЕАЛЬНИЙ стан угоди (/deal/<id>), і лише потім (якщо
    доречно) викликає relist_product() — а не орієнтується тільки на
    відсутність кнопки на сторінці товару.

    Реальний випадок 2026-07-21: "кнопка не знайдено" ховає дві різні
    ситуації — товар ще фізично не передано покупцю (AWAITING_DELIVERY,
    потрібна ручна дія продавця) і угода "Завершено", але кошти в
    ~48-годинному замороженні (COMPLETED, retry з довгим backoff вирішує
    сам). deal_status.py дає це розрізнення прямим текстом сторінки угоди.
    """
    # started ДО будь-якої перевірки: якщо процес впаде саме тут, у БД
    # лишиться видимий слід "розпочато, не завершено", а не повна
    # відсутність будь-якої інформації.
    db_relist_attempts.mark_relist_started(db_path, order.deal_id)

    deal_status = await deal_status_module.check_deal_status(page, order.deal_id)
    logger.info(
        "[deal_status] %s (%s): стадія=%s текст=%r",
        order.deal_id,
        order.product_name,
        deal_status.stage.value,
        deal_status.raw_text[:120],
    )

    if deal_status.stage == DealStage.AWAITING_DELIVERY:
        # Товар фізично не передано покупцю — жодна кількість retry цього не
        # виправить, це чекає на ручну дію продавця в Roblox. Не витрачаємо
        # retry-спробу, лише переносимо наступну перевірку на 6 год.
        logger.info(
            "Угода %s ще в «Выполнение» — товар не передано, чекаю ручної дії",
            order.deal_id,
        )
        db_relist_attempts.schedule_recheck(
            db_path, order.deal_id, minutes=360, reason="Угода в статусі «Выполнение» — товар не передано"
        )
        if not db_notifications.has_notification_been_sent(
            db_path, deal_id=order.deal_id, notification_type="awaiting_delivery"
        ):
            message = format_awaiting_delivery_message(order)
            sent = await notifier.send(message)
            db_notifications.save_notification(
                db_path,
                channel="telegram",
                notification_type="awaiting_delivery",
                message_text=message,
                success=sent,
                deal_id=order.deal_id,
            )
        return

    if deal_status.stage in (DealStage.PENDING_CONFIRMATION, DealStage.UNKNOWN):
        # Ще не час: або чекає підтвердження покупця (само вирішиться), або
        # статус не вдалось однозначно розпізнати (не вгадуємо — просто
        # спробуємо перечитати пізніше). Жодного Telegram — не важлива подія.
        logger.info(
            "Угода %s: %s (%s) — перевірю пізніше",
            order.deal_id,
            deal_status.stage.value,
            deal_status.raw_text[:100],
        )
        db_relist_attempts.schedule_recheck(
            db_path, order.deal_id, minutes=30, reason=f"Статус угоди: {deal_status.raw_text[:200]}"
        )
        return

    if deal_status.stage == DealStage.REFUNDED:
        # Угоду скасовано/повернено — автоматичний Premium-реліст тут
        # недоречний; той самий шлях "остаточна, не retriable помилка", що й
        # для явно неможливих випадків relist_product().
        relist_result = RelistResult(
            success=False,
            reason="Угода в статусі «Возврат средств» — автоматичний реліст не виконується",
            retriable=False,
        )
    else:
        # DealStage.COMPLETED — покупець підтвердив отримання. Лише тепер
        # доречно перевіряти саму кнопку релісту (кошти можуть ще бути в
        # ~48-годинному замороженні — тоді потрібен довгий backoff, а не
        # хибна "остаточна" помилка).
        async with get_relist_lock(relist_locks, order.product_url):
            relist_result = await relist_product(page, order.product_url)

    outcome = db_relist_attempts.record_relist_outcome(db_path, order.deal_id, relist_result)

    if outcome.status == "relisted":
        logger.info("Перевиставлення товару %s: %s", order.product_url, relist_result.reason)
        success_message = format_relist_success_message(order)
        success_sent = await notifier.send(success_message)
        db_notifications.save_notification(
            db_path,
            channel="telegram",
            notification_type="relist_success",
            message_text=success_message,
            success=success_sent,
            deal_id=order.deal_id,
        )
    elif outcome.status == "pending_relist":
        # Тимчасова невдача (RelistResult.retriable=True) — наступна спроба
        # вже запланована (next_retry_at), Telegram НЕ шлемо: лише фінальний
        # результат (успіх чи вичерпання спроб) вартий сповіщення, інакше
        # кожна ретрая заспамить чат.
        logger.warning(
            "Перевиставлення товару %s не вдалося (спроба %d/%d, буде retry): %s",
            order.product_url,
            outcome.attempt_count,
            1 + db_relist_attempts.MAX_RETRY_ATTEMPTS,
            relist_result.reason,
        )
    else:  # relist_failed — остаточно, спроби вичерпано або помилка не retriable
        logger.error(
            "Перевиставлення товару %s остаточно не вдалося після %d спроб(и): %s",
            order.product_url,
            outcome.attempt_count,
            relist_result.reason,
        )
        failure_message = format_relist_exhausted_message(order, outcome.history)
        failure_sent = await notifier.send(failure_message)
        db_notifications.save_notification(
            db_path,
            channel="telegram",
            notification_type="relist_failed",
            message_text=failure_message,
            success=failure_sent,
            deal_id=order.deal_id,
        )
