"""Перевиставлення проданого/простроченого товару на Playerok.

Ізольований модуль: працює виключно з ОДНИМ конкретним товаром за його
унікальним product_url (з Order.product_url, отриманого динамічно зі
сторінки угоди — ніколи не хардкод ID). Переходить одразу на сторінку цього
товару за URL — не сканує список товарів, не переглядає історію старих
продажів. Гарантія "не чіпати інші лоти" випливає прямо з цього: єдина дія
виконується лише над тим самим product_url, що переданий у виклику.

На сторінці товару Playerok показує різні кнопки залежно від стану:
  - активний товар ("В продаже"): "Поднять в топ" — це НЕ перевиставлення,
    тут не використовується;
  - проданий або з простроченим строком виставлення: "Выставить повторно" —
    саме ця кнопка й використовується;
  - деякі товари не мають кнопки перевиставлення взагалі (лише банер
    "Продан" без пояснення) — перевиставлення для них неможливе.

ПОВНІСТЮ ПЕРЕВІРЕНО НАЖИВО на реальному акаунті (2026-07-18), включно з
реальною оплатою — увесь ланцюжок підтверджено робочим:

  1. Клік "Выставить повторно" відкриває модалку "Выберите сервис". Вона
     вантажиться у ДВА етапи (заголовок з'являється одразу, вміст —
     трохи пізніше, з власним спінером) — тому чекати треба саме появи
     кінцевої кнопки, а не фіксовану паузу.
  2. У модалці вже позначений (checked) варіант "Премиум" — це НЕ чекбокс,
     а <input type="radio" name="itemPriority">, чіпати його не потрібно.
     Кнопка підтвердження — "Выставить Премиум за <ціна>₽" (ціна залежить
     від товару; символ ₽ відділений НЕРОЗРИВНИМ пробілом \xa0, тому
     матчиться лише префікс "Выставить Премиум").
  3. Клік по ній відкриває ДРУГУ модалку "Оплата" з вибором способу оплати
     (за замовчуванням — "Оплатить с баланса", уже увімкнено) і кнопкою
     "Перейти к оплате" — це і є справжній фінальний, платний крок.
  4. Одразу після кліку статус товару стає "В обработке" (перед тим як
     згодом стати "В продаже") — саме "В обработке" використовується як
     сигнал успіху, оскільки з'являється надійно й швидко.

Підтверджено підсумком: баланс продавця зменшився рівно на суму послуги
(25₽ у тесті), товар з'явився першим у списку активних з повним новим
строком виставлення.

За explicit рішенням користувача (2026-07-18): бот ЗАВЖДИ проходить усі три
кроки й підтверджує оплату Преміум-варіанту, без додаткового питання
дозволу на кожне окреме перевиставлення (кошти списуються з балансу
продавця автоматично щоразу).

Надійність (2026-07-19), без зміни самої бізнес-логіки вище:
  - Кожен крок логується (початок/результат) — щоб за логами можна було
    точно сказати, на якому саме кроці стався збій.
  - Повторні спроби — ЛИШЕ для навігації (goto) й очікування появи
    елемента (wait_for): це read-only дії, які безпечно повторити, якщо
    причина — тимчасова мережева затримка. КЛІКИ ніколи не повторюються
    автоматично: повторний клік по кнопці оплати міг би спричинити
    подвійне списання коштів. Якщо клік виконано, а підтвердження після
    нього не прийшло — операція завершується помилкою, а не повторним
    кліком.
  - Якщо очікувана кнопка не з'явилась (інтерфейс Playerok змінився чи
    товар у стані, що не підтримує перевиставлення) — жодного кліку
    навмання, одразу RelistResult(success=False, ...) з причиною.
  - RelistResult.retriable (2026-07-20, реальний випадок: 8/8 живих замовлень
    отримали "кнопку не знайдено", хоча вона з'являлась за кілька годин) —
    ЦЕЙ модуль лишається "одна спроба за виклик", але позначає, чи має сенс
    ВИКЛИКАЮЧОМУ (main.py + db/relist_attempts.py, з exponential backoff)
    спробувати ще раз пізніше. True для тимчасових UI/мережевих збоїв
    (елемент ще не зʼявився), False — лише коли елемент знайдено, але явно
    неактивний (можлива стала причина: обмеження, недостатньо коштів).
  - Після оплати статус перевіряється ДВІЧІ: спершу на вже відкритій
    сторінці (оптимістичне оновлення SPA), а потім ще раз — після повного
    перезавантаження сторінки товару "з нуля", щоб виключити хибний
    позитив від клієнтського стану, що не підтвердився сервером.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

_RELIST_BUTTON_TEXT = "Выставить повторно"
# Ціна після "Выставить Премиум за" залежить від товару — матчимо лише префікс.
_PREMIUM_SUBMIT_BUTTON_PATTERN = re.compile(r"^Выставить Премиум")
_PAY_BUTTON_TEXT = "Перейти к оплате"
# Одразу після оплати статус — "В обработке", і лише згодом стає "В продаже".
_SUCCESS_STATUS_PATTERN = re.compile(r"^(В продаже|В обработке)$")

_ALREADY_ACTIVE_CHECK_TIMEOUT_MS = 7000
_BUTTON_WAIT_TIMEOUT_MS = 10000
_MODAL_WAIT_TIMEOUT_MS = 15000
_PAY_MODAL_WAIT_TIMEOUT_MS = 15000
_CONFIRM_WAIT_TIMEOUT_MS = 15000
_REVERIFY_WAIT_TIMEOUT_MS = 15000

# Ретраї стосуються лише навігації й очікування (read-only) — ніколи кліків.
_NAVIGATION_RETRY_ATTEMPTS = 3
_WAIT_RETRY_ATTEMPTS = 2


@dataclass(frozen=True)
class RelistResult:
    """Результат спроби перевиставлення одного конкретного товару.

    retriable (лише для success=False): чи має сенс спробувати ще раз пізніше.
    True для тимчасових UI/мережевих збоїв (елемент ще не з'явився, timeout) —
    реальний випадок 2026-07-20: Playerok не завжди одразу після продажу
    показує кнопку "Выставить повторно", вона зʼявляється з затримкою. False —
    лише коли елемент ЗНАЙДЕНО, але він явно неактивний (можлива постійна
    причина: обмеження на товар, недостатньо коштів для оплати) — повторювати
    таке без розбору немає сенсу. Повторний виклик у будь-якому разі безпечний
    (ground-truth перевірка на початку), retriable — лише про економію часу
    між спробами (db/relist_attempts.py вирішує, коли саме повторити).
    """

    success: bool
    reason: str
    retriable: bool = True


async def _goto_with_retry(page: Page, url: str, *, attempts: int = _NAVIGATION_RETRY_ATTEMPTS) -> None:
    """Переходить на url, повторюючи спробу при тимчасових мережевих помилках.

    Ретрай тут безпечний: навігація не змінює стан акаунта чи угоди — на
    відміну від кліків, які тут ніколи не повторюються автоматично.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            logger.info("[relist] goto(%s) — спроба %d/%d", url, attempt, attempts)
            await page.goto(url, wait_until="domcontentloaded")
            return
        except PlaywrightTimeoutError as exc:
            last_exc = exc
            logger.warning("[relist] Timeout goto(%s) на спробі %d/%d", url, attempt, attempts)
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "[relist] Мережева помилка goto(%s) на спробі %d/%d: %s", url, attempt, attempts, exc
            )
    assert last_exc is not None
    raise last_exc


async def _wait_with_retry(
    locator: Locator, timeout_ms: int, *, attempts: int = _WAIT_RETRY_ATTEMPTS, label: str
) -> bool:
    """Чекає появу елемента з кількома спробами при тимчасових затримках.

    Це лише очікування (read-only), не клік — повторення тут безпечне: або
    елемент зрештою з'явиться (мережа була повільною), або його справді
    немає (інтерфейс змінився / дія неможлива), і виклик чесно поверне
    False, замість того щоб клікати навмання.
    """
    for attempt in range(1, attempts + 1):
        logger.info("[relist] Очікую «%s» — спроба %d/%d (timeout=%dмс)", label, attempt, attempts, timeout_ms)
        try:
            await locator.wait_for(timeout=timeout_ms)
            logger.info("[relist] «%s» з'явився(лась)", label)
            return True
        except PlaywrightTimeoutError:
            logger.warning("[relist] «%s» не з'явився(лась) за спробу %d/%d", label, attempt, attempts)
    return False


async def relist_product(page: Page, product_url: str) -> RelistResult:
    """Перевиставляє рівно один товар за його унікальним product_url.

    Ніколи не торкається жодного іншого товару: усі дії виконуються лише на
    сторінці, що відповідає переданому product_url. Проходить усі три кроки
    (вибір сервісу -> Преміум -> оплата з балансу) і щоразу списує кошти —
    це свідоме, заздалегідь узгоджене рішення, не питання дозволу щоразу.
    """
    logger.info("[relist] Початок перевиставлення: %s", product_url)

    try:
        await _goto_with_retry(page, product_url)
    except PlaywrightTimeoutError:
        return RelistResult(
            success=False,
            reason=f"Timeout під час відкриття сторінки товару: {product_url}",
        )
    except Exception as exc:
        logger.exception("[relist] Не вдалося відкрити сторінку товару %s", product_url)
        return RelistResult(success=False, reason=f"Не вдалося відкрити сторінку товару: {exc}")

    # count() перевіряє миттєво, без очікування гідратації SPA — на щойно
    # завантаженій сторінці це може дати хибний негатив (текст ще не встиг
    # відрендеритись), тому wait_for з ретраєм замість миттєвого count().
    already_active = await _wait_with_retry(
        page.get_by_text(_SUCCESS_STATUS_PATTERN, exact=True).first,
        _ALREADY_ACTIVE_CHECK_TIMEOUT_MS,
        label="товар уже активний",
    )
    if already_active:
        logger.info("[relist] Товар %s уже активний — перевиставлення не потрібне", product_url)
        return RelistResult(success=True, reason="Товар уже активний — перевиставлення не потрібне")

    # Крок 1: "Выставить повторно" -> модалка "Выберите сервис".
    relist_button = page.get_by_role("button", name=_RELIST_BUTTON_TEXT, exact=True)
    if not await _wait_with_retry(relist_button, _BUTTON_WAIT_TIMEOUT_MS, label=_RELIST_BUTTON_TEXT):
        logger.error("[relist] Кнопку «%s» не знайдено для %s", _RELIST_BUTTON_TEXT, product_url)
        return RelistResult(
            success=False,
            reason=f"Кнопку «{_RELIST_BUTTON_TEXT}» не знайдено — перевиставлення неможливе",
        )
    if not await relist_button.is_enabled():
        logger.error("[relist] Кнопка «%s» неактивна для %s", _RELIST_BUTTON_TEXT, product_url)
        return RelistResult(
            success=False,
            reason=f"Кнопка «{_RELIST_BUTTON_TEXT}» неактивна — перевиставлення неможливе",
            retriable=False,
        )
    logger.info("[relist] Клікаю «%s»", _RELIST_BUTTON_TEXT)
    await relist_button.click()

    # Крок 2: підтвердити варіант "Премиум" (уже обраний за замовчуванням) ->
    # модалка "Оплата". Модалка вантажиться у два етапи, тому чекаємо саме
    # появи кінцевої кнопки, а не фіксовану паузу.
    premium_submit_button = page.get_by_role("button", name=_PREMIUM_SUBMIT_BUTTON_PATTERN)
    if not await _wait_with_retry(
        premium_submit_button, _MODAL_WAIT_TIMEOUT_MS, label="кнопка «Выставить Премиум»"
    ):
        logger.error("[relist] Модалка «Выберите сервис» / кнопка Преміум не з'явилась для %s", product_url)
        return RelistResult(
            success=False,
            reason="Модальне вікно «Выберите сервис» не відкрилось або кнопку Преміум не знайдено",
        )
    premium_button_text = await premium_submit_button.inner_text()
    logger.info("[relist] Клікаю кнопку підтвердження: %r", premium_button_text)
    await premium_submit_button.click()

    # Крок 3: підтвердити оплату з балансу (уже обрано за замовчуванням).
    pay_button = page.get_by_role("button", name=_PAY_BUTTON_TEXT, exact=True)
    if not await _wait_with_retry(pay_button, _PAY_MODAL_WAIT_TIMEOUT_MS, label=_PAY_BUTTON_TEXT):
        logger.error("[relist] Модалка «Оплата» не з'явилась для %s", product_url)
        return RelistResult(
            success=False,
            reason="Модальне вікно «Оплата» не відкрилось — перевиставлення не завершено",
        )
    if not await pay_button.is_enabled():
        logger.error("[relist] Кнопка «%s» неактивна для %s", _PAY_BUTTON_TEXT, product_url)
        return RelistResult(
            success=False,
            reason=f"Кнопка «{_PAY_BUTTON_TEXT}» неактивна — оплату не виконано",
            retriable=False,
        )
    logger.info("[relist] Клікаю «%s» (реальне списання коштів)", _PAY_BUTTON_TEXT)
    await pay_button.click()

    # Підтвердження на вже відкритій сторінці — оптимістичне оновлення SPA,
    # ще не обов'язково підтверджене сервером.
    confirmed_in_place = await _wait_with_retry(
        page.get_by_text(_SUCCESS_STATUS_PATTERN, exact=True).first,
        _CONFIRM_WAIT_TIMEOUT_MS,
        label="статус активного товару (одразу після оплати)",
    )
    if not confirmed_in_place:
        logger.error(
            "[relist] Після оплати статус не підтвердився на сторінці для %s "
            "(можливо, кошти вже списані)",
            product_url,
        )
        return RelistResult(
            success=False,
            reason=(
                "Після оплати статус товару не змінився на «В обработке»/«В продаже» "
                "— результат непідтверджений (можливо, кошти вже списані)"
            ),
        )

    # Друга, незалежна перевірка: перезавантажуємо сторінку товару "з нуля"
    # і перевіряємо статус ще раз — це і є "перевірка, що товар дійсно став
    # активним", а не довіра до клієнтського стану SPA одразу після кліку.
    logger.info("[relist] Перезавантажую сторінку товару для незалежної перевірки статусу")
    try:
        await _goto_with_retry(page, product_url)
    except Exception as exc:
        logger.exception("[relist] Не вдалося перезавантажити сторінку товару %s для перевірки", product_url)
        return RelistResult(
            success=False,
            reason=(
                "Оплату виконано, але не вдалося перезавантажити сторінку товару для "
                f"підтвердження статусу: {exc}"
            ),
        )

    reverified = await _wait_with_retry(
        page.get_by_text(_SUCCESS_STATUS_PATTERN, exact=True).first,
        _REVERIFY_WAIT_TIMEOUT_MS,
        label="статус активного товару (після перезавантаження)",
    )
    if not reverified:
        logger.error(
            "[relist] Оплату виконано, але статус не підтвердився після перезавантаження для %s",
            product_url,
        )
        return RelistResult(
            success=False,
            reason=(
                "Оплату виконано, але після перезавантаження сторінки статус товару "
                "не підтвердився активним"
            ),
        )

    logger.info("[relist] Товар %s успішно перевиставлено (Премиум, з оплатою)", product_url)
    return RelistResult(success=True, reason="Товар успішно перевиставлено (Премиум, з оплатою)")
