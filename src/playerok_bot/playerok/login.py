"""Логін на Playerok: перевірка сесії та очікування ручного входу.

Playerok підтримує вхід лише через VK ID, Google OAuth або email з
одноразовим кодом (без пароля) — жоден із цих способів свідомо не
автоматизується (OAuth-провайдери блокують автоматизацію ботами, а email-код
не варто дублювати кодом заради MVP). Замість цього використовується
персистентний профіль браузера (BrowserSession): якщо сесія вже активна —
бот працює з нею; якщо ні — оператор один раз логіниться вручну у видимому
вікні браузера, після чого cookies зберігаються на диску і наступні запуски
вже будуть залогінені.

Перевірка сесії навмисно НЕ використовує wait_for_load_state("networkidle"):
після входу на Playerok довго тривають фонові мережеві запити, тому
"мережевого спокою" можна не дочекатися.

Один сигнал (лише URL або лише один DOM-елемент) недостатній для висновку
"залогінені" — обидва можуть спрацювати на проміжному/нестабільному стані
рендеру SPA. Тому підтвердження двоетапне: (1) шукаємо ознаку можливого
входу на вже відкритій сторінці, і лише (2) якщо вона з'явилась — окремим
page.goto реально відкриваємо сторінку профілю і перевіряємо, що вона
справді завантажилась, а не повернула нас на /profile/auth.

Під час очікування ручного логіну сторінка НЕ перезавантажується, поки
оператор не завершить вхід: LOGIN_URL відкривається лише один раз, а цикл
очікування спостерігає за вже відкритою сторінкою без goto — інакше введений
оператором код губиться при кожному перезавантаженні.
"""

from __future__ import annotations

import logging
import re
import traceback

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

PLAYEROK_BASE_URL = "https://playerok.com"
LOGIN_URL = f"{PLAYEROK_BASE_URL}/profile/auth"

# Посилання на профіль у шапці сайту: href стабільно змінюється з "/profile/auth"
# на "/profile/<username>/..." одразу як з'являється сесія — незалежно від
# того, встиг клієнтський роутер вже змінити видимий page.url чи ні.
_PROFILE_LINK_SELECTOR = 'a[href^="/profile/"]:not([href="/profile/auth"])'
_PROFILE_HREF_PATTERN = re.compile(r"^/profile/[^/]+")

_LOGIN_SIGNAL_WAIT_TIMEOUT_MS = 5000
_POLL_INTERVAL_MS = 3000

logger = logging.getLogger(__name__)


async def _detect_profile_url(page: Page, timeout_ms: int) -> str | None:
    """Шукає МОЖЛИВУ ознаку входу на вже відкритій сторінці — без page.goto().

    Повертає повний URL сторінки профілю-кандидата (ще не перевірений
    реальним переходом), або None, якщо ознак входу поки немає. Це лише
    попередній сигнал — остаточне підтвердження робить _confirm_profile_page.
    """
    if not page.url.startswith(LOGIN_URL):
        return page.url

    try:
        link = page.locator(_PROFILE_LINK_SELECTOR).first
        await link.wait_for(timeout=timeout_ms)
        href = await link.get_attribute("href")
    except PlaywrightTimeoutError:
        return None

    if href is None or not _PROFILE_HREF_PATTERN.match(href):
        return None
    return f"{PLAYEROK_BASE_URL}{href}"


async def _confirm_profile_page(page: Page, profile_url: str) -> bool:
    """Підтверджує вхід окремим реальним переходом на сторінку профілю.

    Це і є перевірка "доступна лише після входу сторінка справді
    відкривається", а не довіра до проміжного стану SPA.
    """
    logger.info("Підтвердження входу: page.goto(%s)", profile_url)
    try:
        await page.goto(profile_url, wait_until="domcontentloaded")
    except PlaywrightTimeoutError:
        logger.warning("Timeout під час підтвердження доступу до %s", profile_url)
        return False
    except Exception:
        logger.error("Виняток у page.goto(%s):\n%s", profile_url, traceback.format_exc())
        raise
    logger.info("Після підтвердження page.url = %s", page.url)
    return not page.url.startswith(LOGIN_URL)


async def is_logged_in(page: Page) -> bool:
    """Разова надійна перевірка активності сесії.

    Відкриває LOGIN_URL один раз, шукає ознаку можливого входу, і якщо вона
    є — підтверджує реальним переходом на сторінку профілю. Призначена для
    одноразової перевірки (наприклад, на старті); у циклі очікування ручного
    логіну використовуйте ensure_logged_in — вона не перезавантажує сторінку
    без потреби.
    """
    logger.info("Перед page.goto(%s)", LOGIN_URL)
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    except PlaywrightTimeoutError:
        logger.warning("Timeout під час page.goto(%s) — спробуємо перевірити пізніше", LOGIN_URL)
        return False
    except Exception:
        logger.error("Виняток у page.goto(%s):\n%s", LOGIN_URL, traceback.format_exc())
        raise
    logger.info("Після page.goto(%s), поточний page.url = %s", LOGIN_URL, page.url)

    profile_url = await _detect_profile_url(page, _LOGIN_SIGNAL_WAIT_TIMEOUT_MS)
    if profile_url is None:
        return False
    return await _confirm_profile_page(page, profile_url)


async def ensure_logged_in(page: Page) -> None:
    """Гарантує активну сесію, за потреби чекаючи на ручний вхід оператора.

    Якщо сесія вже активна (персистентний профіль зберіг її з минулого
    запуску) — одразу повертає керування. Якщо ні — просить оператора
    увійти вручну у видимому вікні браузера (потрібен запуск з
    BROWSER_HEADLESS=false) і чекає завершення входу.

    Сторінка НЕ перезавантажується, поки оператор не завершить вхід сам:
    цикл лише спостерігає за вже відкритою сторінкою (_detect_profile_url
    нічого не goto'ить). Лише коли з'явиться ознака можливого входу,
    робиться один підтверджувальний page.goto на сторінку профілю — на
    цьому етапі оператор вже закінчив вводити код, тож перехід нічого не
    зіпсує.
    """
    if await is_logged_in(page):
        return

    print(f"Увійдіть у Playerok вручну у відкритому вікні браузера: {LOGIN_URL}")
    while True:
        profile_url = await _detect_profile_url(page, _POLL_INTERVAL_MS)
        if profile_url is not None and await _confirm_profile_page(page, profile_url):
            break
    print("Вхід виконано, сесію збережено в персистентному профілі.")
