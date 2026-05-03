import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import TelegramObject

from app.bot.handlers.memory import memory_router
from app.bot.handlers.message_router import main_router
from app.config import settings
from app.jobs.scheduler import start_scheduler
from app.storage.db import init_db


logger = logging.getLogger(__name__)


class AllowedUserMiddleware(BaseMiddleware):
    """Пропускает обновления только от allowed_user_id. Если не задан — пропускает всех."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if settings.allowed_user_id is not None:
            user = data.get("event_from_user")
            if user is None or user.id != settings.allowed_user_id:
                logger.warning("Blocked update from user_id=%s", getattr(user, "id", None))
                return
        return await handler(event, data)


async def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=settings.log_level.upper(),
    )

    init_db()
    logger.info("Database initialised at %s", settings.db_path)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.update.outer_middleware(AllowedUserMiddleware())
    dp.include_router(main_router)
    dp.include_router(memory_router)

    scheduler = start_scheduler(bot)
    try:
        logger.info("Starting polling (allowed_user_id=%s)", settings.allowed_user_id)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")


if __name__ == "__main__":
    asyncio.run(main())
