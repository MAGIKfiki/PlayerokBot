"""Фоновий backlog-скан — ВИЯВЛЕННЯ угод, пропущених живим order-polling
(main.py), а не окрема реалізація перевиставлення.

Переосмислено 2026-07-21 (аудит надійності): раніше цей модуль сканував
"Мої товари → Завершённые" (список ТОВАРІВ) і мав власну, спрощену копію
логіки перевиставлення — без перевірки стану угоди, без retry/backoff.
Реальний ризик: перша ж тимчасова невдача ("кнопка не знайдена" — саме та
причина, яку щойно виправили для order-потоку) назавжди позначала товар
"не вдалося", і НІКОЛИ не перевірялася знову.

Тепер модуль лише ЗНАХОДИТЬ угоди: сканує "Профіль → Продажи" (вкладки
"Выполнение" і "Завершено" — реальні угоди з deal_id, а не просто товари),
фільтрує за словником King Legacy (playerok/keyword_match.py) і, для кожної
ЩЕ НЕ ВІДОМОЇ боту угоди (db_orders.deal_exists), читає її як звичайне
Order (playerok/orders.py::read_order — той самий код, що й для живих
замовлень) і передає в pipeline.process_order() — ЄДИНУ реалізацію
relist-потоку, спільну з main.py. Жодної окремої логіки перевиставлення
тут більше немає.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from playerok_bot import pipeline
from playerok_bot.db import orders as db_orders
from playerok_bot.db import settings as db_settings
from playerok_bot.notifications.telegram import TelegramNotifier
from playerok_bot.playerok import keyword_match, selectors
from playerok_bot.playerok.deal_status import deal_url_for
from playerok_bot.playerok.orders import open_order, read_order
from playerok_bot.playerok.profile import PLAYEROK_BASE_URL, get_own_profile_username

logger = logging.getLogger(__name__)

_SHOW_MORE_BUTTON_TEXT = "Показать еще"
# Достатньо для 1000+ карток (кожен клік довантажує пачку записів) — з
# запобіжником прогресу нижче (list_sales_cards), а не лише лічильником.
_SHOW_MORE_MAX_CLICKS = 500
# Збільшено 8000→15000 (soak-тест 2026-07-22, той самий клас ризику, що й
# _SHOW_MORE_PROGRESS_MAX_WAIT_MS нижче — захисний запас, хоч і не доведено
# напряму цим конкретним випадком).
_SHOW_MORE_WAIT_TIMEOUT_MS = 15000
# Довший за дефолтний 30с таймаут дій — великі (сотні+ карток) сторінки
# рендеряться повільніше, і сам клік (не лише очікування) реально впирався
# у 30с ліміт наживо 2026-07-21.
_SHOW_MORE_CLICK_TIMEOUT_MS = 45000
_SHOW_MORE_CLICK_RETRY_ATTEMPTS = 3
# Активне очікування зростання кількості карток після кліку, замість
# одноразової фіксованої паузи (High #2, soak-тест 2026-07-21/22).
#
# ДОВЕДЕНО інструментуванням (не гіпотеза) 2026-07-22 06:17: реальний цикл
# сканування вкладки "confirmed" (де насправді ~930-954 угод) клікнув
# «Показать еще» рівно ОДИН раз, кнопка спрацювала, але _wait_for_card_growth
# зі старим лімітом 6000мс не дочекався зростання кількості карток і хибно
# визнав список вичерпаним лише на 6 картках (перша сторінка) — тоді як у
# сусідніх циклах (той самий день, ті самі умови) повна пагінація через 135-147
# кліків забирала ~9-10 хвилин, тобто ~65-70мс/клік у середньому, але окремі
# батчі явно вимагали набагато більше за 6с. Збільшено з великим запасом.
_SHOW_MORE_PROGRESS_POLL_INTERVAL_MS = 300
_SHOW_MORE_PROGRESS_MAX_WAIT_MS = 20000
# Активне очікування ПЕРШОГО реального контенту одразу після page.goto(),
# той самий клас гонки, що вище, тільки на початковому читанні (High #2,
# продовження, soak-тест 2026-07-21): одразу після domcontentloaded SPA ще
# могла не встигнути догідратувати список, і миттєвий links.count() читав 0
# ще до появи хоч однієї картки чи кнопки «Показать еще» — цикл тоді хибно
# вважав список порожнім (0 карток, 0 кліків), без жодної помилки в логах.
# Підтверджено наживо: 3 з 5 циклів після рестарту дали 0/0 на обох вкладках,
# тоді як сусідні цикли в той самий час показували 6 і 18 карток. Той самий
# патерн, що вже успішно використовується в orders.py::find_new_order_url —
# таймаут тут не помилка, а трактується як дійсно порожній список.
_CONTENT_WAIT_TIMEOUT_MS = 10000

# ТИМЧАСОВЕ розслідування (soak-тест 2026-07-21): навіть після фіксу вище
# links.count()==0 з'явився знову (2 з 3 циклів на "confirmed" — хибно,
# 3-й цикл тим часом реально знайшов 48 карток). За вимогою користувача — не
# гадати причину, а зібрати технічні докази: скільки саме часу зайняло
# очікування картки й кнопки окремо, що саме "розбудило" очікування,
# document.readyState, URL після навігації, і (лише коли count()==0) повний
# HTML сторінки й скріншот. Видалити цей блок і _diagnose_empty_tab() після
# завершення розслідування — це не постійна частина архітектури.
_DEBUG_ARTIFACT_DIR = Path("/tmp/backlog_scan_debug")

# "Продажи" вкладки, варті сканування: Выполнение (?status=paid, товар ще не
# передано — може виявитись пропущеним живим замовленням) і Завершено
# (?status=confirmed — угода вже завершена, готова до перевиставлення).
# Подтверждение/Возврат средств свідомо не скануються — низька цінність.
_STATUS_QUERIES: tuple[str | None, ...] = ("paid", "confirmed")


@dataclass
class BacklogScanOutcome:
    discovered: list[tuple[str, str]] = field(default_factory=list)  # (product_name, deal_id)

    @property
    def has_anything_new(self) -> bool:
        return bool(self.discovered)


async def _click_show_more_if_present(page: Page) -> bool:
    """Повертає True, якщо клікнуто (є ще картки), False — якщо кнопки
    більше немає (список вичерпано)."""
    show_more = page.get_by_role("button", name=_SHOW_MORE_BUTTON_TEXT, exact=True)
    try:
        await show_more.wait_for(timeout=_SHOW_MORE_WAIT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return False

    # .click() сам чекає на visible+enabled+stable — окремий is_enabled()
    # перед ним лише додає зайвий round-trip і сам ставав джерелом timeout
    # на великих сторінках (перевірено наживо 2026-07-21). Ретрай — лише
    # для самого кліку "показати ще" (read-only за суттю, нічого не платить
    # і не змінює на Playerok), а не для кліків усередині relist_product().
    for attempt in range(1, _SHOW_MORE_CLICK_RETRY_ATTEMPTS + 1):
        try:
            await show_more.click(timeout=_SHOW_MORE_CLICK_TIMEOUT_MS)
            return True
        except PlaywrightTimeoutError:
            logger.warning(
                "[backlog_scan] Клік «%s» не вдався (спроба %d/%d)",
                _SHOW_MORE_BUTTON_TEXT,
                attempt,
                _SHOW_MORE_CLICK_RETRY_ATTEMPTS,
            )
    return False


async def _wait_for_card_growth(page: Page, links: Locator, previous_count: int) -> int:
    """Активно чекає (з опитуванням, не однією фіксованою паузою), поки
    links.count() перевищить previous_count після кліку «Показать еще», до
    _SHOW_MORE_PROGRESS_MAX_WAIT_MS. Повертає фінальну кількість карток —
    так і не зрослу кількість після повного очікування трактуємо як реальне
    вичерпання списку, а не як повільний рендер."""
    elapsed_ms = 0
    current_count = await links.count()
    while current_count <= previous_count and elapsed_ms < _SHOW_MORE_PROGRESS_MAX_WAIT_MS:
        await page.wait_for_timeout(_SHOW_MORE_PROGRESS_POLL_INTERVAL_MS)
        elapsed_ms += _SHOW_MORE_PROGRESS_POLL_INTERVAL_MS
        current_count = await links.count()
    return current_count


async def _timed_wait(locator: Locator, timeout_ms: int) -> tuple[bool, float]:
    """Діагностичний хелпер (тимчасовий, див. коментар біля _DEBUG_ARTIFACT_DIR):
    повертає (matched, elapsed_ms) — чи дочекались елемента і скільки це
    зайняло, замість простого True/False."""
    started_at = time.monotonic()
    try:
        await locator.wait_for(timeout=timeout_ms)
        return True, (time.monotonic() - started_at) * 1000
    except PlaywrightTimeoutError:
        return False, (time.monotonic() - started_at) * 1000


async def _diagnose_empty_tab(page: Page, status_query: str | None) -> None:
    """Тимчасова діагностика (див. коментар біля _DEBUG_ARTIFACT_DIR): коли
    links.count() повернув 0 навіть після активного очікування контенту,
    зберігає повний HTML сторінки й скріншот — щоб довести технічно, чи
    контент дійсно не завантажився, чи DOM інший, чи Playwright дивився не
    туди. Помилки самої діагностики не повинні впливати на скан."""
    try:
        _DEBUG_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        tag = status_query or "paid"
        html = await page.content()
        (_DEBUG_ARTIFACT_DIR / f"{stamp}_{tag}.html").write_text(html, encoding="utf-8")
        await page.screenshot(path=str(_DEBUG_ARTIFACT_DIR / f"{stamp}_{tag}.png"), full_page=True)
        logger.warning(
            "[backlog_scan][DIAG] count()==0 після очікування контенту (tab=%s) — "
            "збережено артефакти: %s_%s.(html|png)",
            tag, stamp, tag,
        )
    except Exception:
        logger.exception("[backlog_scan][DIAG] Не вдалося зберегти діагностичні артефакти")


async def list_sales_cards(page: Page, username: str, status_query: str | None) -> list[tuple[str, str]]:
    """Повертає [(deal_id, raw_card_text), ...] з усіх сторінок вкладки
    "Продажи" за статусом. Клікає "Показать еще" до вичерпання, з
    відстеженням ПРОГРЕСУ (кількість карток) — якщо після кліку й активного
    очікування (_wait_for_card_growth) кількість так і не зросла, вважаємо
    список вичерпаним, навіть якщо кнопка технічно ще клікабельна (запобіжник
    від зависання на дуже великих списках)."""
    url = f"{PLAYEROK_BASE_URL}/profile/{username}/sales"
    if status_query:
        url += f"?status={status_query}"

    goto_started_at = time.monotonic()
    await page.goto(url, wait_until="domcontentloaded")
    goto_elapsed_ms = (time.monotonic() - goto_started_at) * 1000

    links = page.locator(selectors.ORDER_CARD_LINK)
    show_more = page.get_by_role("button", name=_SHOW_MORE_BUTTON_TEXT, exact=True)

    # Обидва очікування — паралельно (asyncio.gather), а не послідовно: інакше
    # найгірший випадок (обидва не з'явились) коштував би 2×_CONTENT_WAIT_TIMEOUT_MS
    # замість одного. Кожне окремо дає власний matched/elapsed_ms для діагностики.
    (card_matched, card_elapsed_ms), (button_matched, button_elapsed_ms) = await asyncio.gather(
        _timed_wait(links.first, _CONTENT_WAIT_TIMEOUT_MS),
        _timed_wait(show_more, _CONTENT_WAIT_TIMEOUT_MS),
    )
    if card_matched and button_matched:
        woke_by = "card" if card_elapsed_ms <= button_elapsed_ms else "button"
    elif card_matched:
        woke_by = "card"
    elif button_matched:
        woke_by = "button"
    else:
        woke_by = "none (timeout)"

    previous_count = await links.count()

    try:
        ready_state = await page.evaluate("document.readyState")
    except Exception as exc:
        ready_state = f"<не вдалося прочитати: {exc}>"

    logger.info(
        "[backlog_scan][DIAG] tab=%s url_after_nav=%s goto_ms=%.0f "
        "card_wait=%s(%.0fms) button_wait=%s(%.0fms) woke_by=%s readyState=%s count_after_wait=%d",
        status_query or "paid",
        page.url,
        goto_elapsed_ms,
        card_matched,
        card_elapsed_ms,
        button_matched,
        button_elapsed_ms,
        woke_by,
        ready_state,
        previous_count,
    )

    if previous_count == 0:
        await _diagnose_empty_tab(page, status_query)

    clicks = 0
    for _ in range(_SHOW_MORE_MAX_CLICKS):
        clicked = await _click_show_more_if_present(page)
        if not clicked:
            break
        clicks += 1

        new_count = await _wait_for_card_growth(page, links, previous_count)
        if new_count <= previous_count:
            break  # навіть після активного очікування карток не побільшало — справді вичерпано
        previous_count = new_count
    else:
        logger.warning(
            "[backlog_scan] %s: досягнуто ліміт кліків «%s» (%d) — можливо, список ще не вичерпано",
            url,
            _SHOW_MORE_BUTTON_TEXT,
            _SHOW_MORE_MAX_CLICKS,
        )

    count = await links.count()
    logger.info("[backlog_scan] %s: «%s» клікнуто %d раз(ів), карток знайдено: %d", url, _SHOW_MORE_BUTTON_TEXT, clicks, count)

    cards: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index in range(count):
        try:
            link = links.nth(index)
            href = await link.get_attribute("href")
            if href is None or href in seen:
                continue
            seen.add(href)
            deal_id = href.removeprefix("/deal/")
            text = (await link.inner_text()).strip()
            cards.append((deal_id, text))
        except Exception as exc:
            # Одна погана картка (тимчасовий Playwright/мережевий збій серед
            # сотень+ послідовних викликів) не повинна валити весь скан —
            # пропускаємо саме її, решта карток обробляється далі. Вона
            # просто розглянеться знову наступного циклу (deal_exists ще не
            # знає про неї).
            logger.warning(
                "[backlog_scan] Не вдалося прочитати картку #%d/%d — пропускаю: %s",
                index,
                count,
                exc,
            )
    return cards


# Захист від тихого прийняття заниженого результату пагінації (soak-тест
# 2026-07-22): навіть після збільшення _SHOW_MORE_PROGRESS_MAX_WAIT_MS живі
# цикли інколи давали 160-450 карток замість реальних ~985-1118 — без жодного
# "досягнуто ліміт кліків", кореляція з мережевою деградацією того вечора
# (кластер playwright_error/network_loss + повторювані "Гідратація логіну не
# завершилась" у ці ж години), а не з архітектурним багом пагінації як таким.
# Продажі не зникають самі по собі, тому різке падіння відносно раніше
# побаченого максимуму — сигнал незавершеного скану, а не новий факт.
_HIGH_WATER_MARK_THRESHOLD = 0.6
_HIGH_WATER_MARK_RETRY_ATTEMPTS = 3


def _high_water_mark_key(status_query: str | None) -> str:
    return f"backlog_high_water_mark_{status_query or 'paid'}"


async def _scan_tab_with_high_water_mark_guard(
    page: Page, username: str, status_query: str | None, db_path: Path
) -> list[tuple[str, str]]:
    """Скановує вкладку (list_sales_cards), але не приймає результат мовчки,
    якщо він підозріло низький порівняно з будь-коли раніше побаченим
    максимумом для цієї ж вкладки (< _HIGH_WATER_MARK_THRESHOLD) — натомість
    логує WARNING і повторює скан із нуля, до _HIGH_WATER_MARK_RETRY_ATTEMPTS
    разів. Перший скан для вкладки (максимум ще 0) завжди приймається —
    немає з чим порівнювати."""
    key = _high_water_mark_key(status_query)
    high_water_mark = db_settings.get_int_setting(db_path, key, 0)

    cards: list[tuple[str, str]] = []
    for attempt in range(1, _HIGH_WATER_MARK_RETRY_ATTEMPTS + 1):
        cards = await list_sales_cards(page, username, status_query)
        count = len(cards)

        if high_water_mark == 0 or count >= high_water_mark * _HIGH_WATER_MARK_THRESHOLD:
            if count > high_water_mark:
                db_settings.set_setting(db_path, key, str(count))
            return cards

        logger.warning(
            "[backlog_scan] Вкладка %s: підозріло низький результат (%d карток, "
            "максимум раніше — %d) — імовірна незавершена пагінація, повторюю "
            "скан із нуля (спроба %d/%d)",
            status_query or "paid",
            count,
            high_water_mark,
            attempt,
            _HIGH_WATER_MARK_RETRY_ATTEMPTS,
        )

    logger.warning(
        "[backlog_scan] Вкладка %s: після %d спроб результат так і лишився "
        "низьким (%d карток, максимум раніше — %d) — приймаю як є, щоб не "
        "блокувати цикл назавжди",
        status_query or "paid",
        _HIGH_WATER_MARK_RETRY_ATTEMPTS,
        len(cards),
        high_water_mark,
    )
    return cards


async def run_backlog_scan(
    page: Page,
    notifier: TelegramNotifier,
    db_path: Path,
    relist_locks: dict[str, asyncio.Lock],
    deal_locks: dict[str, asyncio.Lock],
    *,
    dry_run: bool,
) -> BacklogScanOutcome:
    """Один прохід виявлення: знаходить угоди за словником King Legacy, про
    які бот ще не знає, і передає їх у pipeline.process_order() — ЄДИНУ
    реалізацію relist-потоку, спільну з живим order-polling.

    dry_run — лише безпечний перемикач "виявити й залогувати" на час
    перевірки самого механізму виявлення; не окрема гілка бізнес-логіки.

    backlog_test_limit (settings, 0 = вимкнено) — тимчасове обмеження
    кількості угод, переданих у pipeline.process_order() за весь час дії
    ліміту (лічильник backlog_test_processed_count у settings переживає
    рестарти й окремі цикли скану). Це ЄДИНА відмінність від звичайного
    режиму — сам виклик pipeline.process_order() для дозволених угод
    абсолютно ідентичний (deal_status check, retry/backoff, relist_product,
    усі існуючі захисти, Telegram-сповіщення). Угоди понад ліміт просто
    пропускаються цього разу (не позначаються оброблими) — знову
    розглядатимуться в майбутніх сканах, коли ліміт підняти чи вимкнути.
    """
    outcome = BacklogScanOutcome()
    username = await get_own_profile_username(page)

    test_limit = db_settings.get_int_setting(db_path, "backlog_test_limit", 0)
    processed_count = db_settings.get_int_setting(db_path, "backlog_test_processed_count", 0)

    for status_query in _STATUS_QUERIES:
        cards = await _scan_tab_with_high_water_mark_guard(page, username, status_query, db_path)
        logger.info("[backlog_scan] Вкладка %s: %d угод у списку", status_query or "paid", len(cards))

        for deal_id, raw_text in cards:
            if db_orders.deal_exists(db_path, deal_id):
                continue  # вже відома боту — далі опікується order-polling

            match = keyword_match.classify_name(raw_text)
            if not match.matched:
                continue  # нерелевантна нова угода (напр. Robux) — не King Legacy

            logger.info(
                "[backlog_scan] Нова угода за словником King Legacy: %s (ключове слово: %r)",
                deal_id,
                match.matched_keyword,
            )

            if dry_run:
                logger.info("[backlog_scan] DRY-RUN — обробив би угоду %s", deal_id)
                outcome.discovered.append((raw_text[:60], deal_id))
                continue

            if test_limit > 0 and processed_count >= test_limit:
                logger.info(
                    "[backlog_scan] Тестовий ліміт (%d) досягнуто — угода %s лишається "
                    "непроцесованою до підняття ліміту",
                    test_limit,
                    deal_id,
                )
                continue

            await open_order(page, deal_url_for(deal_id))
            try:
                order = await read_order(page)
            except Exception as exc:
                logger.warning("[backlog_scan] Не вдалося прочитати угоду %s: %s", deal_id, exc)
                continue

            await pipeline.process_order(page, notifier, db_path, order, relist_locks, deal_locks)
            outcome.discovered.append((order.product_name, order.deal_id))

            if test_limit > 0:
                processed_count += 1
                db_settings.set_setting(db_path, "backlog_test_processed_count", str(processed_count))
                logger.info(
                    "[backlog_scan] Тестовий режим: оброблено %d/%d",
                    processed_count,
                    test_limit,
                )

    return outcome
