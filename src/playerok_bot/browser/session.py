"""Керування життєвим циклом браузера Playwright.

Відповідальність цього модуля — лише запуск/зупинка браузера та видача
сторінок (Page). Жодної логіки, специфічної для Playerok чи Roblox, тут бути
не повинно.

Використовує launch_persistent_context із вбудованим у Playwright Chromium і
власним профілем у user_data_dir (окремим від особистого Google Chrome
користувача) — так бот повністю незалежний від того, чи запущений і чим
зайнятий особистий браузер. Cookies та localStorage зберігаються в
user_data_dir на диску, тому залогінена сесія (наприклад, на Playerok)
переживає перезапуск процесу без окремого коду збереження/відновлення.
"""

from __future__ import annotations

from types import TracebackType

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import Playwright as AsyncPlaywright

from playerok_bot.config import BrowserConfig


class BrowserSession:
    """Асинхронний контекст-менеджер над персистентним контекстом Playwright."""

    def __init__(self, config: BrowserConfig) -> None:
        self._config = config
        self._playwright: AsyncPlaywright | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> "BrowserSession":
        self._playwright = await async_playwright().start()
        self._config.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._config.user_data_dir),
            headless=self._config.headless,
            slow_mo=self._config.slow_mo_ms,
        )
        self._context.set_default_timeout(self._config.default_timeout_ms)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def new_page(self) -> Page:
        if self._context is None:
            raise RuntimeError("BrowserSession ще не запущено (використовуйте 'async with')")
        return await self._context.new_page()
