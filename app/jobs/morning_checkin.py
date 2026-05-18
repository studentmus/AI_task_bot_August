import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings

logger = logging.getLogger(__name__)


def _sleep_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(i), callback_data=f"sleep_rate:{i}") for i in range(1, 6)],
        [InlineKeyboardButton(text=str(i), callback_data=f"sleep_rate:{i}") for i in range(6, 11)],
    ])


async def send_sleep_checkin(bot: Bot) -> None:
    user_id = settings.allowed_user_id
    if not user_id:
        return
    try:
        await bot.send_message(
            user_id,
            "☀️ Как поспал сегодня? Оцени качество сна (1 = ужас, 10 = идеально):",
            reply_markup=_sleep_kb(),
        )
        logger.info("Sleep check-in sent to user=%s", user_id)
    except Exception:
        logger.exception("Failed to send sleep check-in")
