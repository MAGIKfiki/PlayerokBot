"""Надсилання повідомлень у Telegram через Bot API.

Єдина відповідальність цього модуля — доставка вже сформованого тексту в
конкретний Telegram-чат. Асинхронний HTTP (httpx), щоб не блокувати той самий
event loop, у якому працює Playwright.
"""

from __future__ import annotations

import logging

import httpx

from playerok_bot.notifications.base import Notifier

logger = logging.getLogger(__name__)

_API_BASE_URL = "https://api.telegram.org"
_REQUEST_TIMEOUT_SECONDS = 10.0

# Telegram Bot API ліміт — 4096 символів на повідомлення (реальний випадок
# 2026-07-21: перший прохід backlog-скану знайшов десятки King Legacy
# товарів одразу, звіт перевищив ліміт і НЕ надіслався взагалі — "text is
# too long"). Запас нижче ліміту — на випадок, якщо розбиття зрізає рядок
# не рівно на межі символу в багатобайтовій послідовності Telegram.
_MAX_MESSAGE_LENGTH = 3800


def _split_into_chunks(text: str, max_length: int) -> list[str]:
    """Ділить текст на частини по межах рядків, кожна ≤ max_length.

    Рядок, довший за max_length сам по собі (малоймовірно для наших
    повідомлень), розбивається "наосліп" по символах — краще так, ніж
    втратити повідомлення повністю.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1  # +1 за "\n", що додасться при join
        if current_len + line_len > max_length and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        while line_len > max_length:
            chunks.append(line[: max_length - 1])
            line = line[max_length - 1 :]
            line_len = len(line) + 1
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


class TelegramNotifier(Notifier):
    """Надсилає повідомлення в один Telegram-чат через Bot API."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id

    async def send(self, text: str) -> bool:
        """Надсилає текстове повідомлення. Ніколи не кидає виняток — лише логує помилку.

        Довший за ліміт Telegram текст автоматично ділиться на кілька
        послідовних повідомлень (за межами рядків) — інакше Telegram просто
        відхиляє все повідомлення цілком ("text is too long").

        Повертає True лише якщо Telegram підтвердив доставку (HTTP 2xx) для
        УСІХ частин, інакше False.
        """
        if not self._bot_token or not self._chat_id:
            logger.warning(
                "Telegram не налаштований (відсутній bot_token або chat_id) — "
                "повідомлення не надіслано"
            )
            return False

        chunks = _split_into_chunks(text, _MAX_MESSAGE_LENGTH)
        url = f"{_API_BASE_URL}/bot{self._bot_token}/sendMessage"
        all_sent = True
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            for index, chunk in enumerate(chunks, start=1):
                prefix = f"[{index}/{len(chunks)}]\n" if len(chunks) > 1 else ""
                payload = {"chat_id": self._chat_id, "text": prefix + chunk}
                try:
                    response = await client.post(url, json=payload)
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    logger.error(
                        "Telegram API повернув помилку %s: %s",
                        exc.response.status_code,
                        exc.response.text,
                    )
                    all_sent = False
                except httpx.HTTPError as exc:
                    logger.error("Не вдалося надіслати повідомлення в Telegram: %s", exc)
                    all_sent = False
        return all_sent
