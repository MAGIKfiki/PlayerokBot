"""Визначення власного профілю продавця — спільне для orders.py й backlog_scan.py.

Винесено окремо, щоб обидва модулі використовували ту саму, вже перевірену
наживо, race-safe логіку читання username із шапки сайту, а не дублювали її.
"""

from __future__ import annotations

import re

from playwright.async_api import Page, expect

from playerok_bot.playerok import selectors

PLAYEROK_BASE_URL = "https://playerok.com"

_OWN_PROFILE_HREF_PATTERN = re.compile(r"^/profile/([^/]+)")
# Одразу після domcontentloaded шапка сайту ще може показувати дефолтний
# href "/profile/auth" (до того, як клієнтський JS підвантажить реальний
# стан логіну) — тому чекаємо саме на реальний username, а не /profile/auth.
_LOGGED_IN_PROFILE_HREF_PATTERN = re.compile(r"^/profile/(?!auth(?:/|$))[^/]+")
_USERNAME_WAIT_TIMEOUT_MS = 10000


async def get_own_profile_username(page: Page) -> str:
    """Дістає власний username із href посилання "Профіль" у шапці сайту.

    Це читання атрибута (без кліку) — потрібне лише для побудови прямого URL
    сторінок профілю, не для навігації кліками.

    Одразу після domcontentloaded href цього посилання інколи ще дефолтний
    "/profile/auth" (гідратація клієнта, що підвантажує реальний стан логіну,
    ще не завершилась) — тому чекаємо (через expect(...).to_have_attribute,
    вбудований поллінг Playwright, без asyncio.sleep), поки href не стане
    реальним username.
    """
    await page.goto(PLAYEROK_BASE_URL, wait_until="domcontentloaded")

    profile_link = page.locator(selectors.PROFILE_LINK).first
    try:
        await expect(profile_link).to_have_attribute(
            "href", _LOGGED_IN_PROFILE_HREF_PATTERN, timeout=_USERNAME_WAIT_TIMEOUT_MS
        )
    except AssertionError:
        pass  # остаточну валідність href все одно перевіряємо нижче — без винятку

    href = await profile_link.get_attribute("href")
    if href is None:
        raise RuntimeError("Не знайдено посилання на профіль у шапці сайту")
    match = _OWN_PROFILE_HREF_PATTERN.match(href)
    if match is None:
        raise RuntimeError(f"Неочікуваний формат посилання на профіль: {href!r}")
    username = match.group(1)
    if username == "auth":
        # Гідратація логіну не завершилась навіть після очікування — href
        # так і лишився дефолтним "/profile/auth". Раніше це мовчки
        # приймалось як "реальний" username (2026-07-21, задокументовано в
        # soak-тесті: обидві вкладки backlog-скану звернулись на
        # /profile/auth/sales і мовчки повернули 0 угод без жодної помилки).
        # Тепер — явна помилка замість тихого невалідного значення.
        raise RuntimeError(
            "Гідратація логіну не завершилась — посилання на профіль досі "
            f"вказує на /profile/auth (timeout={_USERNAME_WAIT_TIMEOUT_MS}ms)"
        )
    return username
