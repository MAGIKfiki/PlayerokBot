"""Персистентність спроб перевиставлення — двофазна (started -> completed),
тепер із retry-чергою для тимчасових збоїв (RelistResult.retriable=True).

Чому двофазна: якщо процес аварійно завершиться ПІСЛЯ mark_relist_started,
але ДО фіксації результату (тобто рівно всередині виклику relist_product()),
у БД лишається видимий "розпочато, не завершено" рядок — а не повна
відсутність будь-якого сліду. Це і є "не втрачати інформацію про поточне
замовлення" при аварійному завершенні.

Чому це безпечно ретраїти: relist_product() перед кожним кліком спершу сам
перевіряє РЕАЛЬНИЙ поточний стан товару на Playerok (чи він уже активний), а
не покладається на припущення з БД — тому повторний виклик (і після
рестарту, і за розкладом retry) ніколи не спричинить подвійного кліку/оплати:
якщо попередня спроба насправді вже довела оплату до кінця, повторний виклик
побачить товар активним і одразу поверне success=True без жодного кліку.

Retry-політика (переглянута 2026-07-21, після технічного аудиту реальної
причини): "кнопка не знайдена" сама по собі НЕ розрізняє дві принципово
різні ситуації (playerok/deal_status.py перевіряє це ПЕРЕД кожною спробою,
за станом самої угоди /deal/<id>, а не лише за відсутністю кнопки):
  - угода ще "Выполнение" (товар не передано) — ручна дія продавця, жодна
    кількість retry цього не виправить (schedule_recheck, не витрачає спробу);
  - угода "Завершено", але кошти в ~48-годинному замороженні на боці
    Playerok (перевірено наживо 2026-07-21: покупець підтвердив отримання,
    а кнопки релісту досі немає) — це питання часу. Тому ramp починається з
    30 хв і виходить на "раз на добу, поки не вдасться", а не здається
    через ~4 години, як у першій версії цієї retry-логіки.

record_relist_outcome сам вирішує:
  - success=True                              -> 'relisted' (фінал).
  - success=False, retriable=True, є ще спроби -> 'pending_relist' + заплановано
    наступну спробу (next_retry_at, ramp -> добовий steady-state).
  - success=False, (retriable=False АБО спроби вичерпано) -> 'relist_failed'
    (фінал) — і повертається ПОВНА історія причин усіх спроб для Telegram.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from playerok_bot.db.connection import connect
from playerok_bot.playerok.relist import RelistResult

_STARTED_REASON = "Спроба розпочата (ще не завершена)"

# Ramp (30 хв, 2 год, 6 год, 12 год, 24 год) — достатньо швидкий на старті й
# водночас реально покриває ~48-годинне замороження коштів Playerok. Після
# ramp — сталий режим "раз на добу, поки не вдасться" (_STEADY_STATE_MINUTES),
# а не жорстка здача через кілька годин. _MAX_STEADY_STATE_ATTEMPTS — щедра,
# але не нескінченна стеля (~30 діб steady-state): якщо навіть після цього
# не вдалось, це вже, найімовірніше, не питання замороження коштів, а щось
# інше, що варто показати людині, а не мовчки ретраїти вічно.
_RAMP_MINUTES: tuple[int, ...] = (30, 120, 360, 720, 1440)
_STEADY_STATE_MINUTES = 1440
_MAX_STEADY_STATE_ATTEMPTS = 30
MAX_RETRY_ATTEMPTS = len(_RAMP_MINUTES) + _MAX_STEADY_STATE_ATTEMPTS


def _next_backoff_minutes(attempt_count: int) -> int:
    if attempt_count <= len(_RAMP_MINUTES):
        return _RAMP_MINUTES[attempt_count - 1]
    return _STEADY_STATE_MINUTES


@dataclass(frozen=True)
class AttemptLogEntry:
    attempt_number: int
    reason: str
    attempted_at: str


@dataclass(frozen=True)
class RelistOutcome:
    status: str  # 'relisted' | 'pending_relist' | 'relist_failed'
    attempt_count: int
    history: tuple[AttemptLogEntry, ...]


def mark_relist_started(db_path: Path, deal_id: str) -> None:
    """Ідемпотентно фіксує початок спроби ДО виклику relist_product().

    ON CONFLICT(order_id) DO NOTHING: якщо рядок для цього замовлення вже є
    (перша спроба чи вже запланований retry) — нічого не змінює, не створює
    дубль. Викликати перед КОЖНОЮ спробою (і першою, і повторною) безпечно.
    """
    with connect(db_path) as conn:
        order_row = conn.execute(
            "SELECT id, product_id FROM orders WHERE deal_id = ?", (deal_id,)
        ).fetchone()
        if order_row is None:
            raise ValueError(f"Замовлення з deal_id={deal_id!r} не знайдено в БД")

        conn.execute(
            """
            INSERT INTO relist_attempts (
                order_id, product_id, success, reason, attempt_count, attempt_history_json
            )
            VALUES (?, ?, NULL, ?, 0, '[]')
            ON CONFLICT (order_id) DO NOTHING
            """,
            (order_row["id"], order_row["product_id"], _STARTED_REASON),
        )


def record_relist_outcome(
    db_path: Path, deal_id: str, result: RelistResult, *, cost_amount: float | None = None
) -> RelistOutcome:
    """Фіксує результат спроби й вирішує, що далі: фінал чи запланований retry."""
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT ra.id, ra.attempt_count, ra.attempt_history_json
            FROM relist_attempts ra JOIN orders o ON o.id = ra.order_id
            WHERE o.deal_id = ?
            """,
            (deal_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Немає розпочатої спроби relist для deal_id={deal_id!r}")

        history = json.loads(row["attempt_history_json"] or "[]")
        attempt_count = row["attempt_count"] + 1
        attempted_at_row = conn.execute("SELECT CURRENT_TIMESTAMP AS now").fetchone()
        attempted_at = attempted_at_row["now"]
        history.append(
            {"attempt_number": attempt_count, "reason": result.reason, "attempted_at": attempted_at}
        )
        history_json = json.dumps(history, ensure_ascii=False)

        if result.success:
            status = "relisted"
            conn.execute(
                """
                UPDATE relist_attempts
                SET success = 1, reason = ?, cost_amount = ?, attempt_count = ?,
                    attempt_history_json = ?, next_retry_at = NULL, attempted_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (result.reason, cost_amount, attempt_count, history_json, row["id"]),
            )
            conn.execute(
                """
                UPDATE orders
                SET bot_status = 'relisted', relisted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE deal_id = ?
                """,
                (deal_id,),
            )
        elif result.retriable and attempt_count <= MAX_RETRY_ATTEMPTS:
            status = "pending_relist"
            backoff_minutes = _next_backoff_minutes(attempt_count)
            conn.execute(
                """
                UPDATE relist_attempts
                SET success = NULL, reason = ?, attempt_count = ?, attempt_history_json = ?,
                    next_retry_at = datetime(CURRENT_TIMESTAMP, ?), attempted_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (result.reason, attempt_count, history_json, f"+{backoff_minutes} minutes", row["id"]),
            )
            conn.execute(
                "UPDATE orders SET bot_status = 'pending_relist', updated_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
                (deal_id,),
            )
        else:
            status = "relist_failed"
            conn.execute(
                """
                UPDATE relist_attempts
                SET success = 0, reason = ?, attempt_count = ?, attempt_history_json = ?,
                    next_retry_at = NULL, attempted_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (result.reason, attempt_count, history_json, row["id"]),
            )
            conn.execute(
                "UPDATE orders SET bot_status = 'relist_failed', updated_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
                (deal_id,),
            )

        return RelistOutcome(
            status=status,
            attempt_count=attempt_count,
            history=tuple(
                AttemptLogEntry(
                    attempt_number=entry["attempt_number"],
                    reason=entry["reason"],
                    attempted_at=entry["attempted_at"],
                )
                for entry in history
            ),
        )


def schedule_recheck(db_path: Path, deal_id: str, *, minutes: int, reason: str) -> None:
    """Переносить наступну перевірку угоди, НЕ витрачаючи retry-спробу і не
    викликаючи relist_product() — для станів, де Premium-реліст свідомо ще
    не пробується (playerok/deal_status.py: AWAITING_DELIVERY — товар не
    передано, потрібна ручна дія продавця; PENDING_CONFIRMATION/UNKNOWN —
    ще не час перевіряти кнопку).

    Вимагає вже існуючого рядка relist_attempts (mark_relist_started
    викликається перед цим у main.py) — лише оновлює next_retry_at/reason.
    """
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE relist_attempts
            SET reason = ?, next_retry_at = datetime(CURRENT_TIMESTAMP, ?)
            WHERE order_id = (SELECT id FROM orders WHERE deal_id = ?)
            """,
            (reason, f"+{minutes} minutes", deal_id),
        )
        conn.execute(
            "UPDATE orders SET bot_status = 'pending_relist', updated_at = CURRENT_TIMESTAMP WHERE deal_id = ?",
            (deal_id,),
        )
