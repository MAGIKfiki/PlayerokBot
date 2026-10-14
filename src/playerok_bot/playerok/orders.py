"""Читання замовлень (угод) продавця на Playerok.

Лише читання: відкрити список нових замовлень, знайти перше, відкрити його й
повернути структуровані дані. Жодних дій, що змінюють стан угоди
(підтвердження виконання, скасування, повідомлення покупцю) тут немає і бути
не повинно.

Навігація йде прямими URL (page.goto), а не кліками по меню: кліки по тексту
на кшталт "Профіль"/"Продажи" крихкі (напр. кілька елементів з однаковим
текстом ламають Playwright strict mode) і залежать від верстки навігації.
Єдине, що потрібно прочитати без прямого URL — власний username, який
дістається з href посилання в шапці сайту (атрибут, не клік).

wait_for_load_state("networkidle") тут також не використовується — на
Playerok постійно тривають фонові мережеві запити (як і на сторінці логіну),
тому "мережевого спокою" можна не дочекатися. Замість цього — goto з
wait_until="domcontentloaded" плюс очікування конкретного елемента там, де
потрібно відрізнити "ще не завантажилось" від "список справді порожній".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from playerok_bot.playerok import selectors
from playerok_bot.playerok.profile import PLAYEROK_BASE_URL, get_own_profile_username

_CONTENT_WAIT_TIMEOUT_MS = 10000

# Playerok deal id — глобально унікальний UUID у /deal/<uuid>, головний
# природний ключ угоди для персистентності (db/orders.py).
_DEAL_ID_PATTERN = re.compile(re.escape(PLAYEROK_BASE_URL) + r"/deal/([^/?#]+)")


@dataclass(frozen=True)
class Order:
    deal_id: str
    product_name: str
    product_url: str
    amount: str
    buyer_name: str
    conversation_url: str
    status: str


async def open_new_orders(page: Page) -> None:
    """Відкриває список угод продавця, що очікують дій (вкладка "Продажи").

    Прямий перехід на /profile/<username>/sales — без кліків по меню.
    """
    username = await get_own_profile_username(page)
    await page.goto(
        f"{PLAYEROK_BASE_URL}/profile/{username}/sales", wait_until="domcontentloaded"
    )


async def find_new_order_url(page: Page) -> str | None:
    """Повертає URL першого замовлення зі списку, або None, якщо список порожній.

    Коротке очікування появи хоча б однієї картки замовлення відрізняє
    "SPA ще не встигла відрендерити список" від "список справді порожній":
    таймаут тут не помилка, а трактується як порожній список.
    """
    order_links = page.locator(selectors.ORDER_CARD_LINK)
    try:
        await order_links.first.wait_for(timeout=_CONTENT_WAIT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return None

    href = await order_links.first.get_attribute("href")
    return f"{PLAYEROK_BASE_URL}{href}" if href else None


async def open_order(page: Page, order_url: str) -> None:
    """Відкриває сторінку конкретної угоди."""
    await page.goto(order_url, wait_until="domcontentloaded")


async def read_order(page: Page) -> Order:
    """Зчитує структуровані дані з уже відкритої сторінки угоди (/deal/<id>).

    Playwright-локатори (get_attribute/inner_text/wait_for) самі чекають на
    появу елемента в DOM до стандартного timeout контексту — окремого
    очікування "мережевого спокою" тут не потрібно.
    """
    chat_link = page.locator(selectors.CHAT_LINK)
    buyer_link = (
        chat_link.locator(f"xpath={selectors.BUYER_LIST_ANCESTOR_XPATH}")
        .locator(selectors.PROFILE_LINK)
        .first
    )
    product_link = page.locator(selectors.PRODUCT_LINK).filter(has_text=re.compile(r"\S")).first
    status_el = page.locator(selectors.STATUS_XPATH)
    amount_el = page.get_by_text(selectors.AMOUNT_TEXT_PATTERN).first

    chat_href = await chat_link.get_attribute("href")
    if chat_href is None:
        raise RuntimeError("Не знайдено посилання на чат з покупцем на сторінці угоди")

    product_href = await product_link.get_attribute("href")
    if product_href is None:
        raise RuntimeError("Не знайдено посилання на товар на сторінці угоди")

    deal_id_match = _DEAL_ID_PATTERN.match(page.url)
    if deal_id_match is None:
        raise RuntimeError(f"Неочікуваний URL сторінки угоди: {page.url!r}")

    return Order(
        deal_id=deal_id_match.group(1),
        product_name=(await product_link.inner_text()).strip(),
        product_url=f"{PLAYEROK_BASE_URL}{product_href}",
        amount=(await amount_el.inner_text()).strip(),
        buyer_name=(await buyer_link.inner_text()).strip(),
        conversation_url=f"{PLAYEROK_BASE_URL}{chat_href}",
        status=(await status_el.inner_text()).strip(),
    )


async def find_and_read_new_order(page: Page) -> Order | None:
    """Знаходить перше нове замовлення, відкриває його й повертає дані.

    Повертає None, якщо нових замовлень немає. Читання лише: жодних дій зі
    зміни стану угоди тут не виконується.
    """
    await open_new_orders(page)
    order_url = await find_new_order_url(page)
    if order_url is None:
        return None
    await open_order(page, order_url)
    return await read_order(page)
