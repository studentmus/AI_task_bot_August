import logging
import re

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.states import PingState
from app.llm.obsidian_tools import append_to_bot_log

logger = logging.getLogger(__name__)

memory_router = Router(name="memory")

# Words that mean "skip this ping for now"
_PING_SKIP_RE = re.compile(
    r"^(?:не\s+сейчас|потом|позже|пропусти(?:те)?|пропустить|skip|не\s+надо|отмена|cancel)\s*$",
    re.IGNORECASE,
)


@memory_router.message(StateFilter(PingState.waiting_response), ~F.text.startswith("/"))
async def handle_ping_response(message: Message, state: FSMContext) -> None:
    """Intercept text while the bot is waiting for a scheduled-ping reply.

    Excludes slash commands so /stop, /sleep etc. fall through to their handlers
    in log_router even while in PingState.
    """
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришли текстовый ответ — запишу.")
        return

    # User wants to skip the ping
    if _PING_SKIP_RE.match(text):
        await state.clear()
        await message.answer("Хорошо, пропустил.")
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
