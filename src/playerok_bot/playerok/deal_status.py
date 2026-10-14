"""Перевірка стану угоди (/deal/<id>) — ОБОВ'ЯЗКОВО перед кожною спробою
Premium-релісту, а не лише орієнтуючись на відсутність кнопки на сторінці
товару.

Чому це важливо (реальний випадок 2026-07-21): сама лише відсутність
кнопки "Выставить повторно" НЕ розрізняє дві принципово різні ситуації:
  - угода ще в статусі "Выполнение" — товар ФІЗИЧНО не передано покупцю
    (потрібна ручна дія продавця в Roblox: жоден retry цього не виправить,
    і витрачати на це спроби — марно);
  - угода "Завершено" (покупець підтвердив отримання), але кошти ще у
    ~48-годинному замороженні на боці Playerok — Playerok не показує
    кнопку релісту, поки цей період не мине. Це питання часу: достатньо
    довгий backoff (db/relist_attempts.py) вирішує це сам.

Сторінка угоди прямим текстом повідомляє, яка це ситуація — значно
надійніше й інформативніше, ніж вгадувати з самої відсутності кнопки на
сторінці товару.

Перевірені наживо точні формулювання (2026-07-21):
  - AWAITING_DELIVERY: заголовок "Активная продажа" + "Выполните заказ"
  - COMPLETED: заголовок "Завершенная продажа" + "Завершено" +
    "Покупатель подтвердил получение товара"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from playerok_bot.playerok.profile import PLAYEROK_BASE_URL

_STATUS_WAIT_TIMEOUT_MS = 15000

# Порядок перевірки — від найспецифічнішого до найзагальнішого: "Завершено"
# як окреме слово міг би випадково збігтися десь у розмітці, тому інші,
# конкретніші фрази перевіряються першими.
_AWAITING_DELIVERY_PATTERN = re.compile(r"Выполните заказ")
_REFUNDED_PATTERN = re.compile(r"Возврат средств")
_PENDING_CONFIRMATION_PATTERN = re.compile(r"Подтверждение получения|Ожидает подтверждения")
_COMPLETED_PATTERN = re.compile(r"Завершенная продажа|Завершено")

# Об'єднаний патерн лише для АКТИВНОГО очікування (не для класифікації):
# domcontentloaded настає ДО того, як SPA відрендерить реальний контент
# (перевірено наживо 2026-07-21 — миттєве body.inner_text() одразу після
# goto зчитувало ще не оновлену сторінку й хибно "бачило" COMPLETED замість
# AWAITING_DELIVERY). Чекаємо появи БУДЬ-ЯКОЇ з відомих фраз перед тим, як
# взагалі читати текст — так само, як увесь інший код проєкту ніколи не
# довіряє миттєвому стану одразу після навігації (playerok/relist.py).
_ANY_KNOWN_STATUS_PATTERN = re.compile(
    r"Выполните заказ|Возврат средств|Подтверждение получения|"
    r"Ожидает подтверждения|Завершенная продажа|Завершено"
)


class DealStage(Enum):
    AWAITING_DELIVERY = "awaiting_delivery"  # "Выполнение" — товар не передано
    PENDING_CONFIRMATION = "pending_confirmation"  # очікує підтвердження покупця
    COMPLETED = "completed"  # "Завершено"
    REFUNDED = "refunded"  # "Возврат средств"
    UNKNOWN = "unknown"  # не вдалось однозначно розпізнати — не вгадуємо


@dataclass(frozen=True)
class DealStatus:
    stage: DealStage
    raw_text: str


def deal_url_for(deal_id: str) -> str:
    return f"{PLAYEROK_BASE_URL}/deal/{deal_id}"


async def check_deal_status(page: Page, deal_id: str) -> DealStatus:
    """Відкриває сторінку угоди й визначає стадію за видимим текстом.

    Read-only: жодного кліку. Активно ЧЕКАЄ появи будь-якої з відомих фраз
    статусу (SPA рендерить контент ПІСЛЯ domcontentloaded, не одночасно з
    ним) — і лише потім читає текст, щоб не зчитати ще не оновлену сторінку.
    Якщо жодна фраза так і не з'явилась — UNKNOWN, а не вгадування: виклик у
    main.py для UNKNOWN просто спробує ще раз пізніше, не вважаючи це ні
    успіхом, ні остаточною помилкою.
    """
    await page.goto(deal_url_for(deal_id), wait_until="domcontentloaded")

    status_indicator = page.get_by_text(_ANY_KNOWN_STATUS_PATTERN).first
    try:
        await status_indicator.wait_for(timeout=_STATUS_WAIT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass  # жодна відома фраза не з'явилась — читаємо що є, дасть UNKNOWN

    text = await page.locator("body").inner_text()

    if _AWAITING_DELIVERY_PATTERN.search(text):
        return DealStatus(DealStage.AWAITING_DELIVERY, "Выполните заказ")
    if _REFUNDED_PATTERN.search(text):
        return DealStatus(DealStage.REFUNDED, "Возврат средств")
    if _PENDING_CONFIRMATION_PATTERN.search(text):
        return DealStatus(DealStage.PENDING_CONFIRMATION, "Очікує підтвердження покупця")
    if _COMPLETED_PATTERN.search(text):
        return DealStatus(DealStage.COMPLETED, "Завершено")

    return DealStatus(DealStage.UNKNOWN, text[:300])
