import logging

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey

from app.bot.states import PingState
from app.config import settings

logger = logging.getLogger(__name__)


async def send_scheduled_ping(
    bot: Bot,
    storage: BaseStorage,
    question: str,
    filename: str,
) -> None:
    """Send a question to the owner and put the bot into PingState.waiting_response.

    The FSM stores which file to write to, so the reply handler knows where to
    append the answer without any extra lookup.
    """
    user_id = settings.allowed_user_id
    if user_id is None:
        logger.warning("send_scheduled_ping: allowed_user_id not set, skipping")
        return

    key = StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id)
    ctx = FSMContext(storage=storage, key=key)

    current = await ctx.get_state()

    # Already waiting for a ping — skip to avoid duplicate prompts
    if current == PingState.waiting_response:
        logger.info("Ping skipped (already in PingState): file=%s", filename)
        return

    # User is mid-logging — set PingState but tell them logging is paused
    in_log = current is not None and current.startswith("LogState:")
    await ctx.set_state(PingState.waiting_response)
    await ctx.update_data(filename=filename)

    text = question
    if in_log:
        text += "\n\n_(режим логирования приостановлен — ответь на вопрос, потом нажми кнопку сферы снова)_"

    await bot.send_message(user_id, text)
    logger.info("Ping sent → file=%s (interrupted_log=%s)", filename, in_log)
