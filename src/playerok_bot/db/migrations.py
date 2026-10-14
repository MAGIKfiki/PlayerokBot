"""Схема бази даних PlayerokBot: впорядкований список міграцій.

Кожен елемент MIGRATIONS — (версія, SQL-скрипт). Застосовуються автоматично
(db/connection.py) при кожному старті бота, за PRAGMA user_version: жодних
ручних дій після git pull не потрібно (лише CREATE TABLE IF NOT EXISTS —
ідемпотентно). Щоб додати таблицю чи колонку в майбутньому — дописати новий
елемент з наступним номером версії, не чіпаючи попередні.

Джерело істини — сирі таблиці (orders, relist_attempts, notifications).
statistics — лише матеріалізований кеш, завжди перебудовний з них.

Час: усі колонки *_at — SQLite CURRENT_TIMESTAMP (завжди UTC, формат
"YYYY-MM-DD HH:MM:SS"). Валюта скрізь за замовчуванням 'RUB', але жодне поле
не є RUB-only — це завжди окрема колонка, а не припущення в коді.
"""

from __future__ import annotations

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS accounts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace          TEXT NOT NULL DEFAULT 'playerok',
    username             TEXT NOT NULL,
    display_name         TEXT,
    browser_profile_path TEXT,
    telegram_chat_id     TEXT,
    is_active            INTEGER NOT NULL DEFAULT 1,
    created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (marketplace, username)
);

CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL REFERENCES accounts (id),
    product_url    TEXT NOT NULL,
    name           TEXT NOT NULL,
    category       TEXT,
    attributes_json TEXT,
    last_known_status TEXT,
    first_seen_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at    TIMESTAMP,
    UNIQUE (account_id, product_url)
);
CREATE INDEX IF NOT EXISTS ix_products_account_id ON products (account_id);
CREATE INDEX IF NOT EXISTS ix_products_category ON products (category);
CREATE INDEX IF NOT EXISTS ix_products_name ON products (name);
CREATE INDEX IF NOT EXISTS ix_products_last_seen_at ON products (last_seen_at);

CREATE TABLE IF NOT EXISTS orders (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id            INTEGER NOT NULL REFERENCES accounts (id),
    deal_id               TEXT NOT NULL UNIQUE,
    deal_url              TEXT NOT NULL,
    product_id            INTEGER NOT NULL REFERENCES products (id),
    product_name_snapshot TEXT NOT NULL,
    amount_raw            TEXT NOT NULL,
    amount_value           REAL,
    currency               TEXT NOT NULL DEFAULT 'RUB',
    buyer_name             TEXT NOT NULL,
    conversation_url       TEXT NOT NULL,
    status_raw              TEXT NOT NULL,
    bot_status TEXT NOT NULL DEFAULT 'discovered'
        CHECK (bot_status IN ('discovered', 'notified', 'relisted', 'relist_failed')),
    discovered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notified_at   TIMESTAMP,
    relisted_at   TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_orders_account_id ON orders (account_id);
CREATE INDEX IF NOT EXISTS ix_orders_product_id ON orders (product_id);
CREATE INDEX IF NOT EXISTS ix_orders_bot_status ON orders (bot_status);
CREATE INDEX IF NOT EXISTS ix_orders_discovered_at ON orders (discovered_at);

CREATE TABLE IF NOT EXISTS relist_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      INTEGER NOT NULL REFERENCES orders (id),
    product_id    INTEGER NOT NULL REFERENCES products (id),
    attempted_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    success       INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    tier          TEXT NOT NULL DEFAULT 'premium',
    cost_amount   REAL,
    currency      TEXT NOT NULL DEFAULT 'RUB'
);
CREATE INDEX IF NOT EXISTS ix_relist_attempts_order_id ON relist_attempts (order_id);
CREATE INDEX IF NOT EXISTS ix_relist_attempts_product_id ON relist_attempts (product_id);
CREATE INDEX IF NOT EXISTS ix_relist_attempts_attempted_at ON relist_attempts (attempted_at);

CREATE TABLE IF NOT EXISTS notifications (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id         INTEGER REFERENCES accounts (id),
    channel            TEXT NOT NULL,
    order_id           INTEGER REFERENCES orders (id),
    notification_type  TEXT NOT NULL,
    message_text        TEXT NOT NULL,
    success             INTEGER NOT NULL,
    error_detail         TEXT,
    sent_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_notifications_order_id ON notifications (order_id);
CREATE INDEX IF NOT EXISTS ix_notifications_account_id ON notifications (account_id);
CREATE INDEX IF NOT EXISTS ix_notifications_channel ON notifications (channel);
CREATE INDEX IF NOT EXISTS ix_notifications_sent_at ON notifications (sent_at);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bot_state (
    account_id               INTEGER PRIMARY KEY REFERENCES accounts (id),
    is_running                INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_at          TIMESTAMP,
    last_successful_cycle_at   TIMESTAMP,
    last_checked_deal_id        TEXT,
    last_login_at                TIMESTAMP,
    last_error                   TEXT,
    last_error_at                 TIMESTAMP,
    state_version                  INTEGER NOT NULL DEFAULT 1,
    updated_at                      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL CHECK (event_type IN (
        'startup', 'shutdown', 'critical_error', 'unhandled_exception',
        'network_loss', 'playwright_error', 'auth_failure', 'crash'
    )),
    account_id  INTEGER REFERENCES accounts (id),
    message     TEXT NOT NULL,
    details_json TEXT,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_logs_event_type ON logs (event_type);
CREATE INDEX IF NOT EXISTS ix_logs_created_at ON logs (created_at);
CREATE INDEX IF NOT EXISTS ix_logs_account_id ON logs (account_id);

CREATE TABLE IF NOT EXISTS statistics (
    account_id                  INTEGER NOT NULL REFERENCES accounts (id),
    date                         TEXT NOT NULL,
    currency                      TEXT NOT NULL DEFAULT 'RUB',
    orders_count                   INTEGER NOT NULL DEFAULT 0,
    orders_amount_sum               REAL NOT NULL DEFAULT 0,
    notifications_sent_count         INTEGER NOT NULL DEFAULT 0,
    notifications_failed_count        INTEGER NOT NULL DEFAULT 0,
    relist_success_count               INTEGER NOT NULL DEFAULT 0,
    relist_failed_count                 INTEGER NOT NULL DEFAULT 0,
    relist_cost_sum                      REAL NOT NULL DEFAULT 0,
    updated_at                            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, date, currency)
);

INSERT OR IGNORE INTO accounts (id, marketplace, username, is_active)
VALUES (1, 'playerok', 'default', 1);
"""

# v2: relist_attempts стає двофазною (started -> completed) для безпечного
# відновлення після аварійного завершення (мідway через перевиставлення):
#   - success тепер NULLABLE ("розпочато, ще не завершено" = NULL);
#   - UNIQUE(order_id) — рівно один рядок на замовлення (idempotent upsert
#     замість append-only), щоб повторний виклик після рестарту не створював
#     дубль, а оновлював той самий рядок.
# SQLite не підтримує ALTER COLUMN/ADD CONSTRAINT — стандартний паттерн:
# нова таблиця -> копіювання -> drop -> rename.
_SCHEMA_V2 = """
CREATE TABLE relist_attempts_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      INTEGER NOT NULL REFERENCES orders (id),
    product_id    INTEGER NOT NULL REFERENCES products (id),
    attempted_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    success       INTEGER,
    reason        TEXT NOT NULL,
    tier          TEXT NOT NULL DEFAULT 'premium',
    cost_amount   REAL,
    currency      TEXT NOT NULL DEFAULT 'RUB',
    UNIQUE (order_id)
);

INSERT INTO relist_attempts_new (
    id, order_id, product_id, attempted_at, success, reason, tier, cost_amount, currency
)
SELECT id, order_id, product_id, attempted_at, success, reason, tier, cost_amount, currency
FROM relist_attempts;

DROP TABLE relist_attempts;
ALTER TABLE relist_attempts_new RENAME TO relist_attempts;

CREATE INDEX IF NOT EXISTS ix_relist_attempts_product_id ON relist_attempts (product_id);
CREATE INDEX IF NOT EXISTS ix_relist_attempts_attempted_at ON relist_attempts (attempted_at);
"""

# v3: backlog-перевиставлення "Завершённых" товарів (окремий фоновий цикл,
# не привʼязаний до конкретного замовлення) — за тим самим двофазним
# started->completed патерном, що й relist_attempts (v2), але ключ тут
# product_id (UNIQUE), бо тут немає deal_id: товар знайдено скануванням
# списку, а не через купівлю. UNIQUE(product_id) — рівно один запис на товар
# НАЗАВЖДИ (і relist, і свідомий skip записуються), тому кожен наступний скан
# просто фільтрує вже оброблені product_id — це і є "пам'ятати оброблені
# товари" та "не платити Premium вдруге після рестарту" в одному місці.
#
# Класифікація — ЛИШЕ за назвою товару (словник King Legacy +
# Robux/поповнення-blacklist, playerok/king_legacy_keywords.json): фото як
# критерій НЕ використовується — перевірено емпірично (2026-07-20), що
# perceptual-hash не розділяє надійно мініатюри цього акаунта, бо користувач
# використовує той самий арт із дівчиною майже на всіх товарах, тож
# фото-подібність не несе розрізняльної інформації.
_SCHEMA_V3 = """
ALTER TABLE bot_state ADD COLUMN last_backlog_scan_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS backlog_relist_attempts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL UNIQUE REFERENCES products (id),
    decision            TEXT NOT NULL CHECK (decision IN ('relist', 'skip')),
    decision_reason     TEXT NOT NULL,
    matched_keyword     TEXT,
    success             INTEGER,
    dry_run             INTEGER NOT NULL DEFAULT 0,
    cost_amount         REAL,
    currency            TEXT NOT NULL DEFAULT 'RUB',
    reviewed_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    attempted_at         TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_backlog_relist_attempts_product_id
    ON backlog_relist_attempts (product_id);
CREATE INDEX IF NOT EXISTS ix_backlog_relist_attempts_decision
    ON backlog_relist_attempts (decision);

-- Налаштування backlog-циклу: змінювані без рестарту й без коду (читаються
-- щоцикл із db/settings.py). INSERT OR IGNORE — не перезаписує, якщо
-- користувач (чи бот через майбутню Telegram-команду) вже їх міняв.
INSERT OR IGNORE INTO settings (key, value) VALUES ('backlog_scan_interval_minutes', '5');
INSERT OR IGNORE INTO settings (key, value) VALUES ('backlog_scan_dry_run', 'true');

-- Retry з exponential backoff для relist_product(): "кнопка не знайдена"
-- та подібні тимчасові UI-збої (playerok/relist.py::RelistResult.retriable)
-- більше НЕ остаточна помилка з першої спроби — реальний випадок 2026-07-20,
-- коли Playerok, судячи з усього, не одразу оновлює кнопку "Выставить
-- повторно" одразу після продажу, і 8/8 живих замовлень отримали
-- relist_failed, хоча кнопка була доступна вже за кілька годин. Новий
-- проміжний статус 'pending_relist' — товар очікує наступної запланованої
-- спроби (next_retry_at), а не остаточно провалений.
--
-- SQLite не підтримує ALTER COLUMN/ADD CONSTRAINT на CHECK — той самий
-- патерн, що й у v2: нова таблиця -> копіювання -> drop -> rename. Нові
-- колонки relist_attempts (attempt_count/next_retry_at/attempt_history_json)
-- додаються звичайним ADD COLUMN — тут рестрикцій CHECK нема, перебудова не
-- потрібна.
--
-- relist_attempts.order_id REFERENCES orders(id) — DROP TABLE orders
-- порушує цей FK, поки PRAGMA foreign_keys=ON (за замовчуванням у
-- db/connection.py). Вимикаємо перевірку лише на час цієї перебудови: самі
-- id зберігаються один в один (INSERT ... SELECT id, ...), тому посилання
-- лишаються коректними — просто SQLite не повинен їх перевіряти в процесі.
PRAGMA foreign_keys = OFF;

CREATE TABLE orders_new (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id            INTEGER NOT NULL REFERENCES accounts (id),
    deal_id               TEXT NOT NULL UNIQUE,
    deal_url              TEXT NOT NULL,
    product_id            INTEGER NOT NULL REFERENCES products (id),
    product_name_snapshot TEXT NOT NULL,
    amount_raw            TEXT NOT NULL,
    amount_value           REAL,
    currency               TEXT NOT NULL DEFAULT 'RUB',
    buyer_name             TEXT NOT NULL,
    conversation_url       TEXT NOT NULL,
    status_raw              TEXT NOT NULL,
    bot_status TEXT NOT NULL DEFAULT 'discovered'
        CHECK (bot_status IN ('discovered', 'notified', 'pending_relist', 'relisted', 'relist_failed')),
    discovered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notified_at   TIMESTAMP,
    relisted_at   TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO orders_new (
    id, account_id, deal_id, deal_url, product_id, product_name_snapshot,
    amount_raw, amount_value, currency, buyer_name, conversation_url,
    status_raw, bot_status, discovered_at, notified_at, relisted_at, updated_at
)
SELECT
    id, account_id, deal_id, deal_url, product_id, product_name_snapshot,
    amount_raw, amount_value, currency, buyer_name, conversation_url,
    status_raw, bot_status, discovered_at, notified_at, relisted_at, updated_at
FROM orders;

DROP TABLE orders;
ALTER TABLE orders_new RENAME TO orders;

CREATE INDEX IF NOT EXISTS ix_orders_account_id ON orders (account_id);
CREATE INDEX IF NOT EXISTS ix_orders_product_id ON orders (product_id);
CREATE INDEX IF NOT EXISTS ix_orders_bot_status ON orders (bot_status);
CREATE INDEX IF NOT EXISTS ix_orders_discovered_at ON orders (discovered_at);

ALTER TABLE relist_attempts ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE relist_attempts ADD COLUMN next_retry_at TIMESTAMP;
ALTER TABLE relist_attempts ADD COLUMN attempt_history_json TEXT;

PRAGMA foreign_keys = ON;
"""

# (версія, SQL). Додавати нові елементи в кінець зі зростаючим номером —
# ніколи не змінювати вже застосовані.
MIGRATIONS: list[tuple[int, str]] = [
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
    (3, _SCHEMA_V3),
]
