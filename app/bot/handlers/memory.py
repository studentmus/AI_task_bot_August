import logging

from aiogram import Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.states import PingState
from app.llm.obsidian_tools import append_to_bot_log

logger = logging.getLogger(__name__)

memory_router = Router(name="memory")


@memory_router.message(StateFilter(PingState.waiting_response))
async def handle_ping_response(message: Message, state: FSMContext) -> None:
    """Intercept any text while the bot is waiting for a scheduled-ping reply.

    Registered before main_router in dp, so this fires with highest priority
    even before NLP guards and the task parser.
    """
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришли текстовый ответ — запишу.")
        return

    data = await state.get_data()
    filename = data.get("filename", "health.md")

    result = await append_to_bot_log(filename, text)
    await state.clear()

    if result.startswith("Ошибка"):
        await message.answer(result)
    else:
        stem = filename.removesuffix(".md")
        await message.answer(f"📝 Записано в {stem}.")
