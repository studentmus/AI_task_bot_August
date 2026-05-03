import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.domain.memory_service import MEMORY_TYPES, MemoryService
from app.storage.db import SessionLocal


logger = logging.getLogger(__name__)

memory_router = Router(name="memory")


# ---------------------------------------------------------------------------
# Proposal: отправить пользователю предложение сохранить память
# ---------------------------------------------------------------------------

def _proposal_keyboard(memory_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Сохранить", callback_data=f"mem_confirm_{memory_id}"),
            InlineKeyboardButton(text="❌ Не сохранять", callback_data=f"mem_reject_{memory_id}"),
        ]
    ])


def _proposal_text(content: str, memory_type: str) -> str:
    type_label = MEMORY_TYPES.get(memory_type, memory_type)
    return (
        f"💡 Запомнить?\n\n"
        f"«{content}»\n\n"
        f"Тип: {type_label}"
    )


async def send_memory_proposal(
    bot: Bot,
    user_id: int,
    memory_id: int,
    content: str,
    memory_type: str = "fact",
) -> None:
    """Отправляет пользователю сообщение с предложением сохранить запись в память."""
    await bot.send_message(
        chat_id=user_id,
        text=_proposal_text(content, memory_type),
        reply_markup=_proposal_keyboard(memory_id),
    )
    logger.info("Memory proposal sent memory_id=%s user=%s", memory_id, user_id)


# ---------------------------------------------------------------------------
# Callbacks: подтвердить / отклонить
# ---------------------------------------------------------------------------

@memory_router.callback_query(F.data.startswith("mem_confirm_"))
async def cb_mem_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    memory_id = int(callback.data.split("_", 2)[2])

    with SessionLocal() as session:
        svc = MemoryService(session)
        try:
            result = svc.confirm(memory_id)
        except ValueError as e:
            await callback.message.edit_text(f"⚠️ {e}")
            return

    await callback.message.edit_text(result)


@memory_router.callback_query(F.data.startswith("mem_reject_"))
async def cb_mem_reject(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    memory_id = int(callback.data.split("_", 2)[2])

    with SessionLocal() as session:
        svc = MemoryService(session)
        try:
            result = svc.reject(memory_id)
        except ValueError as e:
            await callback.message.edit_text(f"⚠️ {e}")
            return

    await callback.message.edit_text(result)
