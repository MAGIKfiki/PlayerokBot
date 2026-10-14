"""Довгоживучий сервіс: логін один раз, далі нескінченний polling нових
замовлень Playerok із Telegram-сповіщенням про кожне нове (рівно один раз) і
подальшим перевиставленням саме купленого товару (playerok/relist.py).

SQLite (db/) — єдине джерело істини про весь стан бота: жодних in-memory
set() для дедуплікації. Кожен цикл опитування СПОЧАТКУ дообробляє незавершені
замовлення з БД (db_orders.get_unfinished_orders — не залежить від живого
списку на Playerok, який могла вже покинути угода), і лише потім шукає нове.
Це і є "джерело істини — БД, а не сторінка": після рестарту (краш, вимкнення
живлення) бот продовжує РІВНО з того місця, де зупинився, за даними з БД.

Сам relist-потік (перевірка стану угоди -> retry/backoff -> Telegram) живе в
playerok_bot/pipeline.py — ЄДИНА реалізація, спільна для щойно знайдених
замовлень (тут) і заднім числом виявлених через backlog-скан
(playerok/backlog_scan.py). Жодного дублювання логіки перевиставлення.

Два незалежні цикли (order-polling і backlog-скан) захищені один від одного
на двох рівнях (аудит 2026-07-21):
  1. Кожен цикл ізолює СВОЮ службову помилку (heartbeat/mark_* тепер
     усередині try/except, а не поза ним) — щоб транзієнтний збій БД в
     ОДНОМУ циклі не міг вирватись назовні й забрати з собою другий.
  2. _supervised() навколо asyncio.gather() — якщо цикл усе ж аварійно
     завершиться (щось, чого try/except усередині не передбачив), його
     перезапускають окремо, а не гасять увесь процес. Self-healing: жоден
     окремий loop не може зупинити бота цілком.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import traceback
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from playerok_bot import pipeline
from playerok_bot.browser.session import BrowserSession
from playerok_bot.config import load_browser_config, load_database_config, load_telegram_config
from playerok_bot.db import bot_state
from playerok_bot.db import logs as db_logs
from playerok_bot.db import orders as db_orders
from playerok_bot.db import settings as db_settings
from playerok_bot.db.connection import ensure_schema
from playerok_bot.notifications.telegram import TelegramNotifier
from playerok_bot.playerok import backlog_scan
from playerok_bot.playerok.login import ensure_logged_in
from playerok_bot.playerok.orders import find_and_read_new_order

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL_MIN_SECONDS = 2.0
_POLL_INTERVAL_MAX_SECONDS = 5.0

_NETWORK_ERROR_MARKERS = ("net::", "econnreset", "etimedout", "enotfound", "network")

# Фрази, якими Playwright повідомляє про ОСТАТОЧНУ втрату браузерної сесії
# (сам браузер/контекст/сторінка фізично закриті) — на відміну від тимчасових
# мережевих збоїв чи таймаутів одного запиту. Реальний випадок 2026-08-01:
# "Page.goto: Target page, context or browser has been closed" — повторювався
# на кожному циклі нескінченно, бо _supervised() перезапускав саму функцію
# циклу, але не саму Page/BrowserContext, яка лишалась мертвою назавжди.
_BROWSER_LOST_MARKERS = (
    "has been closed",
    "target closed",
    "browser has disconnected",
    "connection closed",
)

_BACKLOG_SCAN_MIN_INTERVAL_SECONDS = 60
# Пауза перед перезапуском циклу після аварійного завершення (_supervised) —
# щоб раптовий постійний збій (напр. вичерпане місце на диску) не спамив
# перезапусками сотні разів на секунду.
_LOOP_RESTART_DELAY_SECONDS = 5.0

# Обмежений exponential backoff для ВІДНОВЛЕННЯ цілої браузерної сесії
# (новий BrowserSession + нові Page + повторний логін) — окремо від
# _LOOP_RESTART_DELAY_SECONDS вище, який лише про перезапуск однієї функції
# циклу. Якщо сесія пропрацювала досить довго (_RECOVERY_RESET_THRESHOLD_
# SECONDS) перш ніж знову впасти — це вважається "новим" збоєм, і backoff
# скидається на початкове значення, а не продовжує рости нескінченно.
_RECOVERY_INITIAL_BACKOFF_SECONDS = 5.0
_RECOVERY_MAX_BACKOFF_SECONDS = 300.0
_RECOVERY_RESET_THRESHOLD_SECONDS = 120.0


class BrowserSessionLost(Exception):
    """Браузер/контекст/сторінка остаточно закриті — продовжувати цей цикл
    із тими самими об'єктами Page безглуздо. Навмисно НЕ перехоплюється
    всередині _run_polling_loop/_run_backlog_scan_loop/_supervised() —
    пропускається до main(), де вся браузерна сесія перестворюється
    цілком (нові Page, повторний логін), а не лише перезапускається
    функція циклу зі старою, вже мертвою Page."""


def _looks_like_network_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _NETWORK_ERROR_MARKERS)


def _recovery_sleep_seconds(stored_backoff: float, session_duration_seconds: float) -> float:
    """Скільки секунд чекати перед ЦІЄЮ спробою відновлення браузерної сесії.

    Чиста функція (без I/O) — винесена окремо саме для того, щоб логіку
    "обмеженого exponential backoff, що скидається після досить довгої
    стабільної роботи" можна було перевірити тестами без запуску реального
    asyncio.gather()/Playwright. `stored_backoff` — значення, накопичене з
    попередніх спроб; якщо щойно завершена сесія протрималась довше за
    _RECOVERY_RESET_THRESHOLD_SECONDS, це вважається новим збоєм і
    повертається початкове значення замість накопиченого. Подвоєння
    (з обмеженням _RECOVERY_MAX_BACKOFF_SECONDS) застосовується викликачем
    ПІСЛЯ сну, на основі значення, яке фактично було використано зараз.
    """
    if session_duration_seconds >= _RECOVERY_RESET_THRESHOLD_SECONDS:
        return _RECOVERY_INITIAL_BACKOFF_SECONDS
    return stored_backoff


def _is_browser_session_lost(exc: BaseException) -> bool:
    """Відрізняє ОСТАТОЧНУ втрату браузера від тимчасового мережевого збою
    чи таймауту одного запиту (_looks_like_network_error) — обидва
    трапляються у тому самому except (PlaywrightError, PlaywrightTimeoutError)
    блоці, але вимагають принципово різної реакції: тимчасовий збій можна
    просто повторити на наступному циклі тією самою Page; втрату браузера —
    ні, потрібна зовсім нова сесія (main._run_one_browser_session)."""
    text = str(exc).lower()
    return any(marker in text for marker in _BROWSER_LOST_MARKERS)


def _mark_heartbeat_safely(db_path: Path, event_type: str) -> None:
    """heartbeat/mark_* — це службовий запис "цикл живий", а не результат
    самого циклу. Раніше викликався ПОЗА try/except циклу (main.py, до
    аудиту 2026-07-21): транзієнтна помилка SQLite тут (напр. рідкісний
    "database is locked") виривалась з циклу назовні, крізь asyncio.gather,
    і забирала з собою ввесь процес — попри те, що сам цикл щойно успішно
    все обробив. Тепер ізольовано: збій тут лише логується, ніколи не
    зупиняє цикл."""
    try:
        if event_type == "poll":
            bot_state.mark_heartbeat(db_path)
        elif event_type == "backlog":
            bot_state.mark_backlog_scan(db_path)
    except Exception as exc:
        logger.exception("Не вдалося записати heartbeat (%s) — продовжуємо", event_type)
        try:
            db_logs.log_event(db_path, "unhandled_exception", f"heartbeat write failed: {exc}"[:500])
        except Exception:
            pass  # навіть лог не записався — не даємо цьому зупинити цикл


async def _run_polling_loop(
    page: Page,
    notifier: TelegramNotifier,
    db_path: Path,
    relist_locks: dict[str, asyncio.Lock],
    deal_locks: dict[str, asyncio.Lock],
) -> None:
    """Нескінченно опрацьовує замовлення. Кожен цикл:

    1. Спершу дообробляє все незавершене з БД (db_orders.get_unfinished_orders)
       — це підхоплює замовлення, перервані аварійним завершенням процесу,
       НЕЗАЛЕЖНО від того, чи вони досі видно в списку "нових" на Playerok.
    2. Потім шукає щойно куплене нове замовлення на живій сторінці.

    Дедуплікація — виключно через БД за order.deal_id (pipeline.process_order),
    а сам вхід у process_order() серіалізовано per-deal_id (deal_locks) —
    один deal_id фізично не може обробитись двічі одночасно, навіть якщо
    цей цикл і backlog-цикл побачать його майже в один момент.
    """
    while True:
        try:
            for unfinished_order in db_orders.get_unfinished_orders(db_path):
                logger.info(
                    "Дообробляю незавершене замовлення (після рестарту чи збою): %s",
                    unfinished_order.deal_id,
                )
                await pipeline.process_order(page, notifier, db_path, unfinished_order, relist_locks, deal_locks)

            order = await find_and_read_new_order(page)
            if order is not None:
                await pipeline.process_order(page, notifier, db_path, order, relist_locks, deal_locks)

            try:
                bot_state.mark_successful_cycle(db_path)
            except Exception:
                logger.exception("Не вдалося записати mark_successful_cycle — продовжуємо")
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _is_browser_session_lost(exc):
                logger.critical("Браузерну сесію втрачено під час order-polling: %s", exc)
                db_logs.log_event(db_path, "critical_error", f"[order-polling] Браузерну сесію втрачено: {exc}"[:500])
                bot_state.mark_error(db_path, str(exc)[:500])
                raise BrowserSessionLost(str(exc)) from exc
            logger.exception("Playwright/мережева помилка під час циклу — продовжуємо")
            event_type = "network_loss" if _looks_like_network_error(exc) else "playwright_error"
            db_logs.log_event(db_path, event_type, str(exc)[:500])
            bot_state.mark_error(db_path, str(exc)[:500])
        except Exception as exc:
            logger.exception("Необроблений виняток під час циклу — продовжуємо")
            db_logs.log_event(
                db_path,
                "unhandled_exception",
                str(exc)[:500],
                details={"traceback": traceback.format_exc()},
            )
            bot_state.mark_error(db_path, str(exc)[:500])

        _mark_heartbeat_safely(db_path, "poll")
        await asyncio.sleep(
            random.uniform(_POLL_INTERVAL_MIN_SECONDS, _POLL_INTERVAL_MAX_SECONDS)
        )


async def _run_backlog_scan_loop(
    page: Page,
    notifier: TelegramNotifier,
    db_path: Path,
    relist_locks: dict[str, asyncio.Lock],
    deal_locks: dict[str, asyncio.Lock],
) -> None:
    """Незалежний повільний цикл: сканує "Продажи" на пропущені King Legacy
    угоди (playerok/backlog_scan.py) і передає їх у ТОЙ САМИЙ relist-потік
    (playerok_bot/pipeline.py), що й order-polling.

    Повністю окремий від _run_polling_loop — власна Page (окрема вкладка),
    власний, значно довший інтервал (settings.backlog_scan_interval_minutes,
    перечитується щоцикл — змінюється без рестарту бота), той самий
    try/except-і-продовжити патерн, щоб падіння тут не гасило order-polling.
    """
    while True:
        interval_minutes = db_settings.get_int_setting(db_path, "backlog_scan_interval_minutes", 5)
        dry_run = db_settings.get_bool_setting(db_path, "backlog_scan_dry_run", True)
        try:
            await backlog_scan.run_backlog_scan(
                page, notifier, db_path, relist_locks, deal_locks, dry_run=dry_run
            )
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if _is_browser_session_lost(exc):
                logger.critical("Браузерну сесію втрачено під час backlog-скану: %s", exc)
                db_logs.log_event(db_path, "critical_error", f"[backlog-scan] Браузерну сесію втрачено: {exc}"[:500])
                bot_state.mark_error(db_path, str(exc)[:500])
                raise BrowserSessionLost(str(exc)) from exc
            logger.exception("Playwright/мережева помилка під час backlog-скану — продовжуємо")
            event_type = "network_loss" if _looks_like_network_error(exc) else "playwright_error"
            db_logs.log_event(db_path, event_type, str(exc)[:500])
            bot_state.mark_error(db_path, str(exc)[:500])
        except Exception as exc:
            logger.exception("Необроблений виняток під час backlog-скану — продовжуємо")
            db_logs.log_event(
                db_path,
                "unhandled_exception",
                str(exc)[:500],
                details={"traceback": traceback.format_exc()},
            )
            bot_state.mark_error(db_path, str(exc)[:500])

        _mark_heartbeat_safely(db_path, "backlog")
        await asyncio.sleep(max(_BACKLOG_SCAN_MIN_INTERVAL_SECONDS, interval_minutes * 60))


async def _supervised(name: str, loop_factory, db_path: Path) -> None:
    """Обгортка self-healing: якщо loop_factory() усе ж аварійно завершиться
    (щось, чого власний try/except циклу не передбачив — напр. помилка в
    самому обробнику помилок), перезапускає ЛИШЕ цей цикл, а не весь бот.

    Кожен із двох циклів (_run_polling_loop, _run_backlog_scan_loop) — це
    `while True`, який у теорії ніколи сам не повертається; ця функція —
    зовнішній запобіжник на випадок, якщо теорія колись не спрацює.
    asyncio.CancelledError навмисно НЕ перехоплюється — справжнє скасування
    (напр. Ctrl+C) має завершити цикл, а не "самозцілитись".

    BrowserSessionLost так само НЕ перехоплюється й НЕ лікується тут: немає
    сенсу перезапускати функцію циклу зі старою Page, яка вже остаточно
    закрита. Замість цього виняток проходить крізь asyncio.gather() у
    main._run_one_browser_session() — gather() сам скасовує сусідній
    (другий) цикл, і вся браузерна сесія перестворюється цілком.
    """
    while True:
        try:
            await loop_factory()
            logger.critical(
                "Цикл %s завершився без винятку (неочікувано для `while True`) — перезапускаю", name
            )
        except (asyncio.CancelledError, BrowserSessionLost):
            raise
        except Exception as exc:
            logger.critical("Цикл %s аварійно завершився — перезапускаю: %s", name, exc, exc_info=True)
            try:
                db_logs.log_event(
                    db_path,
                    "unhandled_exception",
                    f"[{name}] loop crashed, restarting: {exc}"[:500],
                    details={"traceback": traceback.format_exc()},
                )
            except Exception:
                pass
        await asyncio.sleep(_LOOP_RESTART_DELAY_SECONDS)


async def _run_one_browser_session(
    config,
    notifier: TelegramNotifier,
    db_path: Path,
    started_flag: list[bool],
) -> None:
    """Один повний життєвий цикл браузерної сесії: новий BrowserSession,
    нові Page, повторна перевірка логіну, обидва робочі цикли.

    Модульна функція (не вкладений closure) — щоб можна було тестувати
    напряму, підмінюючи BrowserSession/ensure_logged_in моками, без запуску
    всього main() і реального Playwright.

    started_flag — одноелементний список-"комірка" замість nonlocal: перший
    успішний логін виставляє started_flag[0] = True, щоб main() знав, що
    is_running слід скинути у фінальному finally (той самий запобіжник, що
    й раніше, — другий невдалий інстанс не повинен стирати is_running=1
    першого, уже живого).

    Завершується виключно винятком — або BrowserSessionLost (сесію навмисно
    не рятуємо тут, main() перехопить і перестворить усе заново), або іншою
    помилкою (напр. не вдалося запустити Chromium, не вдалося увійти).
    asyncio.gather() всередині гарантує, що коли ОДИН із двох циклів впаде
    з BrowserSessionLost, інший буде скасований gather'ом автоматично — не
    продовжить працювати зі своєю окремою Page від уже мертвого контексту.
    """
    async with BrowserSession(config) as session:
        page = await session.new_page()

        try:
            await ensure_logged_in(page)
        except Exception as exc:
            db_logs.log_event(
                db_path,
                "auth_failure",
                f"Не вдалося увійти в Playerok: {exc}",
                details={"traceback": traceback.format_exc()},
            )
            raise
        bot_state.mark_login(db_path)
        bot_state.mark_running(db_path, True)
        started_flag[0] = True

        # Окрема вкладка для backlog-скану — той самий persistent-контекст
        # (уже залогінений), тому повторний логін не потрібен. Окрема
        # Page — щоб повільний скан "Продажи" не заважав навігації
        # швидкого order-polling циклу на основній сторінці.
        backlog_page = await session.new_page()
        # Нові, порожні локи щоразу для нової сесії — локи з попередньої
        # (вже мертвої) сесії нікому більше не потрібні: будь-яка угода,
        # що оброблялась у момент втрати браузера, однаково лишає свій
        # слід у SQLite (mark_relist_started/record_relist_outcome),
        # тому повторна обробка після відновлення безпечна незалежно
        # від того, чи "пам'ятає" про неї конкретний об'єкт asyncio.Lock.
        relist_locks: dict[str, asyncio.Lock] = {}
        # Спільний за order.deal_id, окремо від relist_locks (за
        # product_url) — серіалізує весь pipeline.process_order(), а не
        # лише клік у relist_product(). Без цього щойно збережене
        # backlog-скaном замовлення могло одночасно підхопитись і
        # order-polling циклом (2026-07-23, перший production-тест).
        deal_locks: dict[str, asyncio.Lock] = {}

        print("Логін виконано. Починаю моніторинг нових замовлень (Ctrl+C для зупинки)...")
        await asyncio.gather(
            _supervised(
                "order-polling",
                lambda: _run_polling_loop(page, notifier, db_path, relist_locks, deal_locks),
                db_path,
            ),
            _supervised(
                "backlog-scan",
                lambda: _run_backlog_scan_loop(backlog_page, notifier, db_path, relist_locks, deal_locks),
                db_path,
            ),
        )


async def main() -> None:
    db_path = load_database_config().path
    ensure_schema(db_path)
    db_logs.log_event(db_path, "startup", "Бот запускається")

    config = load_browser_config()
    telegram_config = load_telegram_config()
    notifier = TelegramNotifier(telegram_config.bot_token, telegram_config.chat_id)

    # is_running позначається ЛИШЕ після реального успішного старту (браузер
    # + логін) — і скидається у finally ЛИШЕ якщо саме цей процес його
    # позначив. Інакше невдала спроба запустити ДРУГИЙ інстанс (напр.
    # конфлікт browser-profile, коли перший інстанс уже живий і працює)
    # своїм finally стирає правдивий is_running=1 першого, справді живого
    # процесу — перевірено наживо: саме так і сталося під час тестування.
    # Прапорець живе на рівні ВСІЄЇ роботи процесу (across рестартів
    # браузерної сесії всередині одного й того самого процесу python), а не
    # лише однієї сесії — тому оголошений тут, а не всередині
    # _run_one_browser_session().
    started_flag = [False]

    try:
        backoff = _RECOVERY_INITIAL_BACKOFF_SECONDS
        while True:
            session_started_at = time.monotonic()
            try:
                await _run_one_browser_session(config, notifier, db_path, started_flag)
                logger.critical(
                    "_run_one_browser_session() завершився без винятку (неочікувано) — перезапускаю"
                )
            except asyncio.CancelledError:
                raise
            except BrowserSessionLost as exc:
                logger.critical("Відновлюю браузерну сесію після втрати: %s", exc)
                db_logs.log_event(
                    db_path, "critical_error", f"Відновлення браузерної сесії після втрати: {exc}"[:500]
                )
            except Exception as exc:
                # Сюди потрапляє, зокрема, провал самого запуску Chromium чи
                # помилка під час ensure_logged_in (auth_failure вже
                # залогований окремо вище, тут лише критичний запис і retry).
                logger.critical(
                    "Критична помилка браузерної сесії — повторна спроба: %s", exc, exc_info=True
                )
                db_logs.log_event(
                    db_path,
                    "critical_error",
                    f"Критична помилка браузерної сесії: {exc}"[:500],
                    details={"traceback": traceback.format_exc()},
                )

            session_duration = time.monotonic() - session_started_at
            sleep_for = _recovery_sleep_seconds(backoff, session_duration)
            logger.info("Повторний запуск браузерної сесії через %.0fс", sleep_for)
            await asyncio.sleep(sleep_for)
            backoff = min(sleep_for * 2, _RECOVERY_MAX_BACKOFF_SECONDS)
    finally:
        if started_flag[0]:
            bot_state.mark_running(db_path, False)
        db_logs.log_event(db_path, "shutdown", "Бот завершує роботу")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Зупинено.")
    except Exception:
        # Спрацює лише якщо збій стався поза внутрішніми try/except (напр.
        # сам запуск BrowserSession чи логін) — фіксуємо як crash і піднімаємо
        # далі, щоб traceback у консолі лишився як завжди. _supervised()
        # усередині main() вже захищає обидва робочі цикли від такого;
        # це — останній рубіж для збоїв поза ними (напр. сам логін).
        db_logs.log_event(
            load_database_config().path,
            "crash",
            "Бот аварійно завершився",
            details={"traceback": traceback.format_exc()},
        )
        raise
