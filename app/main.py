import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, TelegramObject

from app.bot.handlers.log_handler import log_router
from app.bot.handlers.memory import memory_router
from app.bot.handlers.message_router import main_router
from app.bot.handlers.motivation import motivation_router
from app.config import settings
from app.jobs.scheduler import start_scheduler
from app.storage.db import init_db
from app.storage.fsm_storage import SQLiteFSMStorage


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


async def _set_commands(bot: Bot) -> None:
    commands = [
        # ── Логирование ──────────────────────────────────────────────────
        BotCommand(command="sleep",    description="🌙 Записать сон (23:30-7:00, с полуночи до 8...)"),
        BotCommand(command="meal",     description="🍽 Записать питание"),
        BotCommand(command="workout",  description="💪 Записать тренировку"),
        BotCommand(command="german",   description="🇩🇪 Записать немецкий (/de)"),
        BotCommand(command="romanian", description="🇷🇴 Записать румынский (/ro)"),
        BotCommand(command="ideas",    description="💡 Записать идею"),
        BotCommand(command="ctx",      description="📊 Записать личную заметку"),
        BotCommand(command="wish",     description="🛒 Записать в список покупок"),
        BotCommand(command="guitar",   description="🎸 Записать игру на гитаре"),
        BotCommand(command="stop",     description="⏹ Выйти из режима логирования"),
        BotCommand(command="undo",     description="↩ Отменить последнюю запись (/undo sleep)"),
        # ── Задачи ───────────────────────────────────────────────────────
        BotCommand(command="pending",   description="📋 Последние задачи в базе"),
        BotCommand(command="recurring", description="🔁 Повторяющиеся задачи"),
        BotCommand(command="cleanup",   description="🧹 Очистить мусорные задачи"),
        # ── Инструменты ──────────────────────────────────────────────────
        BotCommand(command="motivate", description="💢 Мотивационный пинок (/motivate зал)"),
        BotCommand(command="audit",    description="🔍 Последние tool calls (отладка)"),
        BotCommand(command="start",    description="🤖 Справка и список команд"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    logger.info("Bot commands registered (%d)", len(commands))


async def _enrich_old_entries() -> None:
    """Background: extract structured_data for recent entries that don't have it yet.

    Only runs LLM for nutrition/training (sleep is sync). Throttled to 1 req/s.
    Caps at 40 entries to avoid excessive API calls on first start.
    """
    await asyncio.sleep(5)  # let the bot fully start first
    try:
        from app.domain.log_parser import extract_structured
        from app.storage.db import LogEntry, SessionLocal
        from app.storage.log_repo import LogRepo
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        needs_llm = {"nutrition", "training"}
        processed = 0

        with SessionLocal() as session:
            entries = (
                session.query(LogEntry)
                .filter(
                    LogEntry.structured_data.is_(None),
                    LogEntry.logged_at >= cutoff,
                    LogEntry.sphere.in_({"sleep", "nutrition", "training", "energy"}),
                )
                .order_by(LogEntry.logged_at.desc())
                .limit(40)
                .all()
            )
            entry_data = [(e.id, e.sphere, e.raw_text) for e in entries]

        logger.info("Enriching %d old log entries in background", len(entry_data))

        for entry_id, sphere, raw_text in entry_data:
            try:
                data = await extract_structured(sphere, raw_text)
                if data:
                    with SessionLocal() as session:
                        LogRepo(session).update_structured_data(entry_id, data)
                    processed += 1
                if sphere in needs_llm:
                    await asyncio.sleep(1)  # rate-limit LLM calls
            except Exception as exc:
                logger.debug("Enrich failed for entry %s: %s", entry_id, exc)

        logger.info("Background enrichment done: %d/%d entries updated", processed, len(entry_data))
    except Exception:
        logger.exception("_enrich_old_entries failed")


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

    from pathlib import Path
    fsm_db = str(Path(settings.db_path).parent / "fsm.db")
    storage = SQLiteFSMStorage(fsm_db)

    dp = Dispatcher(storage=storage)
    dp.update.outer_middleware(AllowedUserMiddleware())
    dp.include_router(memory_router)     # FSM ping handler — must be first
    dp.include_router(log_router)        # FSM log handler — before NLP router
    dp.include_router(motivation_router) # /motivate command
    dp.include_router(main_router)

    await _set_commands(bot)
    scheduler = start_scheduler(bot, dp.storage)

    # Enrich recent log entries that were saved before structured_data existed.
    # Runs once in background at startup; sleeps 1s between LLM calls to stay polite.
    asyncio.create_task(_enrich_old_entries())

    try:
        logger.info("Starting polling (allowed_user_id=%s)", settings.allowed_user_id)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")


if __name__ == "__main__":
    asyncio.run(main())
