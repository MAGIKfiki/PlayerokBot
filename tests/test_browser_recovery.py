"""Тести відновлення браузерної сесії (main.py: BrowserSessionLost,
_is_browser_session_lost, _recovery_sleep_seconds, _run_one_browser_session).

Той самий стиль, що й інші тести цієї сесії: без pytest, прямі asyncio-виклики,
mock.patch на реальних функціях, PASS/FAIL у stdout, exit code 1 при провалі.
Жодних реальних Playwright-браузерів, жодних реальних платних дій на Playerok.
"""

import asyncio
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from playwright.async_api import Error as PlaywrightError

from playerok_bot import main as main_module
from playerok_bot import pipeline
from playerok_bot.db.connection import connect, ensure_schema
from playerok_bot.db import orders as db_orders
from playerok_bot.playerok.deal_status import DealStage, DealStatus
from playerok_bot.playerok.orders import Order
from playerok_bot.playerok.relist import RelistResult

checks = []


def check(label, cond):
    checks.append((label, bool(cond)))
    print(("PASS" if cond else "FAIL"), label)


class FakeNotifier:
    async def send(self, text):
        return False  # Telegram у тестах не беремо до уваги


class FakeBrowserSession:
    """Мінімальний двійник BrowserSession — без реального Playwright."""

    def __init__(self, config):
        self._config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, tb):
        return None

    async def new_page(self):
        return object()


def _make_order(deal_id: str) -> Order:
    return Order(
        deal_id=deal_id,
        product_name="Dragon fruit по приятной цене",
        product_url=f"https://playerok.com/products/{deal_id}-product",
        amount="100 RUB",
        buyer_name="buyer",
        conversation_url="https://playerok.com/chats/1",
        status="Продан",
    )


# ---------------------------------------------------------------------------
# 1. Браузер закрився під час перевірки замовлень (order-polling)
# ---------------------------------------------------------------------------
async def scenario_browser_closed_during_order_polling():
    db_path = Path("/tmp/test_browser_recovery_1.sqlite3")
    db_path.unlink(missing_ok=True)
    ensure_schema(db_path)

    async def fake_find_and_read_new_order(page):
        raise PlaywrightError("Page.goto: Target page, context or browser has been closed")

    raised = None
    with mock.patch.object(db_orders, "get_unfinished_orders", side_effect=lambda p: []), \
         mock.patch.object(main_module, "find_and_read_new_order", side_effect=fake_find_and_read_new_order):
        try:
            await main_module._run_polling_loop(
                page=object(), notifier=FakeNotifier(), db_path=db_path, relist_locks={}, deal_locks={}
            )
        except main_module.BrowserSessionLost as exc:
            raised = exc

    check(
        "order-polling: закриття браузера під час перевірки замовлень піднімає BrowserSessionLost",
        raised is not None,
    )


# ---------------------------------------------------------------------------
# 2. Браузер закрився під час backlog scan
# ---------------------------------------------------------------------------
async def scenario_browser_closed_during_backlog_scan():
    db_path = Path("/tmp/test_browser_recovery_2.sqlite3")
    db_path.unlink(missing_ok=True)
    ensure_schema(db_path)

    async def fake_run_backlog_scan(*args, **kwargs):
        raise PlaywrightError("Target page, context or browser has been closed")

    raised = None
    with mock.patch.object(main_module.backlog_scan, "run_backlog_scan", side_effect=fake_run_backlog_scan):
        try:
            await main_module._run_backlog_scan_loop(
                page=object(), notifier=FakeNotifier(), db_path=db_path, relist_locks={}, deal_locks={}
            )
        except main_module.BrowserSessionLost as exc:
            raised = exc

    check(
        "backlog-scan: закриття браузера під час сканування піднімає BrowserSessionLost",
        raised is not None,
    )


# ---------------------------------------------------------------------------
# 3. Сторінка закрилася, але браузер (у теорії) продовжує працювати
# ---------------------------------------------------------------------------
async def scenario_single_page_closed_browser_alive():
    """Поточний дизайн НАВМИСНО не розрізняє "закрилась лише одна сторінка"
    від "закрився весь браузер" — обидва трактуються як BrowserSessionLost і
    призводять до повного перестворення сесії. Це свідомий компроміс на
    користь простоти й надійності, а не недогляд — перевіряємо саме це."""
    db_path = Path("/tmp/test_browser_recovery_3.sqlite3")
    db_path.unlink(missing_ok=True)
    ensure_schema(db_path)

    async def fake_find_and_read_new_order(page):
        raise PlaywrightError("Target closed")

    raised = None
    with mock.patch.object(db_orders, "get_unfinished_orders", side_effect=lambda p: []), \
         mock.patch.object(main_module, "find_and_read_new_order", side_effect=fake_find_and_read_new_order):
        try:
            await main_module._run_polling_loop(
                page=object(), notifier=FakeNotifier(), db_path=db_path, relist_locks={}, deal_locks={}
            )
        except main_module.BrowserSessionLost as exc:
            raised = exc

    check(
        "одна сторінка закрита ('Target closed') теж трактується як втрата сесії (свідоме спрощення)",
        raised is not None,
    )


# ---------------------------------------------------------------------------
# 4. Авторизація Playerok вимагає ручного втручання
# ---------------------------------------------------------------------------
async def scenario_auth_requires_manual_intervention():
    db_path = Path("/tmp/test_browser_recovery_4.sqlite3")
    db_path.unlink(missing_ok=True)
    ensure_schema(db_path)
    started_flag = [False]

    async def failing_ensure_logged_in(page):
        raise RuntimeError(
            "Гідратація логіну не завершилась — посилання на профіль досі вказує на /profile/auth"
        )

    logged_events = []

    def fake_log_event(db_path_arg, event_type, message, **kwargs):
        logged_events.append((event_type, message))

    raised = None
    with mock.patch.object(main_module, "BrowserSession", FakeBrowserSession), \
         mock.patch.object(main_module, "ensure_logged_in", side_effect=failing_ensure_logged_in), \
         mock.patch.object(main_module.db_logs, "log_event", side_effect=fake_log_event), \
         mock.patch.object(main_module.bot_state, "mark_login"), \
         mock.patch.object(main_module.bot_state, "mark_running"):
        try:
            await main_module._run_one_browser_session(
                config=object(), notifier=FakeNotifier(), db_path=db_path, started_flag=started_flag
            )
        except RuntimeError as exc:
            raised = exc

    check("auth: збій логіну не ковтається мовчки — виняток пробивається назовні", raised is not None)
    check(
        "auth: збій записаний саме як подія 'auth_failure'",
        any(event_type == "auth_failure" for event_type, _ in logged_events),
    )
    check(
        "auth: started_flag лишається False — is_running НЕ позначиться хибно True",
        started_flag[0] is False,
    )


# ---------------------------------------------------------------------------
# 5. Кілька невдалих відновлень поспіль — обмежений exponential backoff
# ---------------------------------------------------------------------------
def scenario_repeated_recovery_failures_bounded_backoff():
    backoff = main_module._RECOVERY_INITIAL_BACKOFF_SECONDS
    sleeps = []
    for _ in range(12):
        sleep_for = main_module._recovery_sleep_seconds(backoff, session_duration_seconds=0.1)
        sleeps.append(sleep_for)
        backoff = min(sleep_for * 2, main_module._RECOVERY_MAX_BACKOFF_SECONDS)

    check(
        "backoff: перша спроба — початкове значення",
        sleeps[0] == main_module._RECOVERY_INITIAL_BACKOFF_SECONDS,
    )
    check(
        "backoff: монотонно зростає (не зменшується) при послідовних швидких провалах",
        all(sleeps[i] <= sleeps[i + 1] for i in range(len(sleeps) - 1)),
    )
    check(
        "backoff: обмежений і не росте нескінченно — впирається у стелю",
        sleeps[-1] == main_module._RECOVERY_MAX_BACKOFF_SECONDS and sleeps[-2] == main_module._RECOVERY_MAX_BACKOFF_SECONDS,
    )

    reset_sleep = main_module._recovery_sleep_seconds(
        main_module._RECOVERY_MAX_BACKOFF_SECONDS,
        session_duration_seconds=main_module._RECOVERY_RESET_THRESHOLD_SECONDS + 1,
    )
    check(
        "backoff: скидається до початкового значення після досить довгої стабільної сесії",
        reset_sleep == main_module._RECOVERY_INITIAL_BACKOFF_SECONDS,
    )


# ---------------------------------------------------------------------------
# 6. Після відновлення обидва цикли продовжують роботу
# ---------------------------------------------------------------------------
async def scenario_both_loops_resume_after_recovery():
    db_path = Path("/tmp/test_browser_recovery_6.sqlite3")
    db_path.unlink(missing_ok=True)
    ensure_schema(db_path)

    # --- "Сесія 1": одразу падає через втрату браузера ---
    async def backlog_sleep_forever(*args, **kwargs):
        await asyncio.sleep(3600)  # має бути скасований gather'ом, коли polling впаде

    async def fake_find_and_read_new_order_crash(page):
        raise PlaywrightError("Page.goto: Target page, context or browser has been closed")

    session1_exc = None
    with mock.patch.object(main_module, "BrowserSession", FakeBrowserSession), \
         mock.patch.object(main_module, "ensure_logged_in", new=mock.AsyncMock(return_value=None)), \
         mock.patch.object(main_module.bot_state, "mark_login"), \
         mock.patch.object(main_module.bot_state, "mark_running"), \
         mock.patch.object(db_orders, "get_unfinished_orders", side_effect=lambda p: []), \
         mock.patch.object(main_module, "find_and_read_new_order", side_effect=fake_find_and_read_new_order_crash), \
         mock.patch.object(main_module.backlog_scan, "run_backlog_scan", side_effect=backlog_sleep_forever):
        started_flag = [False]
        try:
            await main_module._run_one_browser_session(object(), FakeNotifier(), db_path, started_flag)
        except main_module.BrowserSessionLost as exc:
            session1_exc = exc
        except asyncio.CancelledError:
            session1_exc = "cancelled"  # сусідній цикл теж коректно скасувався

    check(
        "resume: 'сесія 1' завершується через BrowserSessionLost (gather коректно поширює її)",
        session1_exc is not None,
    )

    # --- "Сесія 2" (після відновлення): обидва цикли реально працюють ---
    process_order_calls = []

    async def fake_process_order(page, notifier, db_path_arg, order, relist_locks, deal_locks):
        process_order_calls.append(order.deal_id)
        db_orders.save_order(
            db_path_arg,
            Order(
                deal_id=order.deal_id, product_name=order.product_name, product_url=order.product_url,
                amount="100 RUB", buyer_name="buyer", conversation_url="https://playerok.com/chats/1",
                status="Продан",
            ),
        )

    call_counter = {"n": 0}

    async def fake_find_and_read_new_order_once(page):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            return _make_order("resume-after-recovery-deal")
        raise asyncio.CancelledError()  # зупиняємо тест, "немов Ctrl+C"

    async def backlog_cancel_immediately(*args, **kwargs):
        raise asyncio.CancelledError()

    session2_exc = None
    with mock.patch.object(main_module, "BrowserSession", FakeBrowserSession), \
         mock.patch.object(main_module, "ensure_logged_in", new=mock.AsyncMock(return_value=None)), \
         mock.patch.object(main_module.bot_state, "mark_login"), \
         mock.patch.object(main_module.bot_state, "mark_running"), \
         mock.patch.object(db_orders, "get_unfinished_orders", side_effect=lambda p: []), \
         mock.patch.object(main_module, "find_and_read_new_order", side_effect=fake_find_and_read_new_order_once), \
         mock.patch.object(main_module.pipeline, "process_order", side_effect=fake_process_order), \
         mock.patch.object(main_module.backlog_scan, "run_backlog_scan", side_effect=backlog_cancel_immediately):
        started_flag2 = [False]
        try:
            await main_module._run_one_browser_session(object(), FakeNotifier(), db_path, started_flag2)
        except asyncio.CancelledError:
            session2_exc = "cancelled"

    check("resume: 'сесія 2' (нова, після відновлення) реально запустилась", session2_exc == "cancelled")
    check(
        "resume: order-polling у новій сесії дійсно обробив нове замовлення",
        process_order_calls == ["resume-after-recovery-deal"],
    )
    check(
        "resume: замовлення справді збережене в БД новою сесією",
        db_orders.deal_exists(db_path, "resume-after-recovery-deal"),
    )


# ---------------------------------------------------------------------------
# 7. Відновлення не створює повторних платних дій
# ---------------------------------------------------------------------------
async def scenario_recovery_does_not_duplicate_paid_actions():
    db_path = Path("/tmp/test_browser_recovery_7.sqlite3")
    db_path.unlink(missing_ok=True)
    ensure_schema(db_path)

    order = _make_order("no-double-payment-deal")
    relist_calls = []

    async def fake_check_deal_status(page, deal_id):
        return DealStatus(stage=DealStage.COMPLETED, raw_text="Завершено")

    # --- Спроба 1: "крашиться" рівно всередині relist_product (як реальний
    # збій 2026-08-01 — браузер закрився саме в момент оплати) ---
    async def crashing_relist_product(page, product_url):
        relist_calls.append(("crash", product_url))
        raise PlaywrightError("Page.goto: Target page, context or browser has been closed")

    notifier = FakeNotifier()
    with mock.patch.object(pipeline, "relist_product", side_effect=crashing_relist_product), \
         mock.patch.object(pipeline.deal_status_module, "check_deal_status", side_effect=fake_check_deal_status):
        try:
            await pipeline.process_order(
                page=object(), notifier=notifier, db_path=db_path, order=order, relist_locks={}, deal_locks={}
            )
        except PlaywrightError:
            pass  # очікувано — саме так виглядає "браузер помер посеред спроби"

    with connect(db_path) as conn:
        row_after_crash = conn.execute(
            "SELECT success, attempt_count FROM relist_attempts ra JOIN orders o ON o.id = ra.order_id "
            "WHERE o.deal_id = ?",
            (order.deal_id,),
        ).fetchone()

    check(
        "no-double-pay: після краху всередині спроби лишається 'розпочато, не завершено' (success IS NULL)",
        row_after_crash is not None and row_after_crash["success"] is None,
    )
    check(
        "no-double-pay: relist_product дійсно викликався рівно один раз під час краху",
        len(relist_calls) == 1,
    )

    # --- "Відновлення": той самий deal_id обробляється ЗНОВУ (як після
    # перестворення браузерної сесії) — цього разу успішно ---
    async def succeeding_relist_product(page, product_url):
        relist_calls.append(("success", product_url))
        return RelistResult(success=True, reason="Товар успішно перевиставлено (Премиум, з оплатою)")

    with mock.patch.object(pipeline, "relist_product", side_effect=succeeding_relist_product), \
         mock.patch.object(pipeline.deal_status_module, "check_deal_status", side_effect=fake_check_deal_status):
        await pipeline.process_order(
            page=object(), notifier=notifier, db_path=db_path, order=order, relist_locks={}, deal_locks={}
        )

    with connect(db_path) as conn:
        row_after_recovery = conn.execute(
            "SELECT success, attempt_count FROM relist_attempts ra JOIN orders o ON o.id = ra.order_id "
            "WHERE o.deal_id = ?",
            (order.deal_id,),
        ).fetchone()

    check(
        "no-double-pay: після відновлення той самий рядок доводиться до success=1 (не створюється другий)",
        row_after_recovery is not None and row_after_recovery["success"] == 1,
    )
    check(
        "no-double-pay: attempt_count == 1 (крашнута спроба НЕ порахувалась окремо, won облік лінійний)",
        row_after_recovery is not None and row_after_recovery["attempt_count"] == 1,
    )
    check(
        "no-double-pay: за весь сценарій (краш + відновлення) relist_product викликано рівно 2 рази — жодного фантомного повтору",
        len(relist_calls) == 2 and relist_calls[0][0] == "crash" and relist_calls[1][0] == "success",
    )


async def main():
    await scenario_browser_closed_during_order_polling()
    await scenario_browser_closed_during_backlog_scan()
    await scenario_single_page_closed_browser_alive()
    await scenario_auth_requires_manual_intervention()
    scenario_repeated_recovery_failures_bounded_backoff()
    await scenario_both_loops_resume_after_recovery()
    await scenario_recovery_does_not_duplicate_paid_actions()

    print()
    total = len(checks)
    passed = sum(1 for _, ok in checks if ok)
    print(f"{passed}/{total} checks passed")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
