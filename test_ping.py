"""Manual ping test: starts the bot, fires one ping after 2 s, then keeps polling.

Usage:
    python3 test_ping.py

Reply to the bot message in Telegram, then Ctrl+C when done.
The reply should appear in MyBrain/_bot/health.md.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers.memory import memory_router
from app.bot.handlers.message_router import main_router
from app.config import settings
from app.jobs.ping_service import send_scheduled_ping
from app.storage.db import init_db


async def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level="INFO",
    )

    init_db()

    storage = MemoryStorage()
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)
    dp.include_router(memory_router)
    dp.include_router(main_router)

    async def fire_ping() -> None:
        await asyncio.sleep(2)
        await send_scheduled_ping(
            bot=bot,
            storage=storage,
            question="[ТЕСТ] Как самочувствие сегодня?",
            filename="health.md",
        )
        print("\n>>> Ping sent. Reply in Telegram, then Ctrl+C when done.\n")

    asyncio.create_task(fire_ping())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
