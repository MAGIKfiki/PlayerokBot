"""Тестовий запуск: перевірка логіну і зчитування першого нового замовлення.

Лише читання інформації — жодних дій, що змінюють стан акаунта чи угоди.
"""

from __future__ import annotations

import asyncio
import logging

from playerok_bot.browser.session import BrowserSession
from playerok_bot.config import load_browser_config
from playerok_bot.playerok.login import ensure_logged_in
from playerok_bot.playerok.orders import find_and_read_new_order

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main() -> None:
    config = load_browser_config()
    async with BrowserSession(config) as session:
        page = await session.new_page()

        await ensure_logged_in(page)

        order = await find_and_read_new_order(page)
        if order is None:
            print("Нових замовлень не знайдено.")
            return

        print("Знайдено нове замовлення:")
        print(f"  product_name:     {order.product_name}")
        print(f"  amount:           {order.amount}")
        print(f"  buyer_name:       {order.buyer_name}")
        print(f"  conversation_url: {order.conversation_url}")
        print(f"  status:           {order.status}")


if __name__ == "__main__":
    asyncio.run(main())
