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
    await ctx.set_state(PingState.waiting_response)
    await ctx.update_data(filename=filename)

    await bot.send_message(user_id, question)
    logger.info("Ping sent → file=%s", filename)
