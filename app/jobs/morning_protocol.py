import logging
from datetime import date
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings

logger = logging.getLogger(__name__)

# (item_id, button_label, full_label)
PROTOCOL_ITEMS: list[tuple[str, str, str]] = [
    ("creatine", "💊 Креатин 5г",    "💊 Креатин 5г"),
    ("vacuum",   "🫁 Вакуум",         "🫁 Вакуум"),
    ("workout",  "🏋️ Зарядка",        "🏋️ Зарядка (скакалка + гиря + подтягивания)"),
    ("sleep",    "😴 Отметить сон",   "😴 Отметить сон"),
    ("plan",     "📋 Утвердить план", "📋 Просмотреть и утвердить план на день"),
    ("phone",    "📱 Телефон",        "📱 10 мин в телефоне (награда в конце)"),
]

_state: dict[str, Optional[str]] = {}   # item_id → "done" | "skipped" | None
_protocol_date: Optional[str] = None


def reset_protocol() -> None:
    global _state, _protocol_date
    _state = {item_id: None for item_id, _, _ in PROTOCOL_ITEMS}
    _protocol_date = date.today().isoformat()


def set_item_status(item_id: str, status: str) -> None:
    _state[item_id] = status


def build_protocol_message() -> tuple[str, Optional[InlineKeyboardMarkup]]:
    lines = ["🌅 <b>Утренний протокол</b>\n"]
    rows: list[list[InlineKeyboardButton]] = []

    done_count = 0
    for item_id, btn_label, full_label in PROTOCOL_ITEMS:
        status = _state.get(item_id)
        if status == "done":
            lines.append(f"✅ {full_label}")
            done_count += 1
        elif status == "skipped":
            lines.append(f"⏭ {full_label}")
            done_count += 1
        else:
            lines.append(f"▫️ {full_label}")
            rows.append([
                InlineKeyboardButton(text=f"✅ {btn_label}", callback_data=f"proto:done:{item_id}"),
                InlineKeyboardButton(text="⏭",               callback_data=f"proto:skip:{item_id}"),
            ])

    total = len(PROTOCOL_ITEMS)
    if not rows:
        real_done = sum(1 for v in _state.values() if v == "done")
        lines.append(f"\n🎉 Готово! {real_done}/{total} выполнено.")
        return "\n".join(lines), None

    remaining = total - done_count
    lines.append(f"\nОсталось: {remaining}/{total}")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def send_morning_protocol(bot: Bot) -> None:
    user_id = settings.allowed_user_id
    if not user_id:
        return
    reset_protocol()
    text, kb = build_protocol_message()
    try:
        await bot.send_message(user_id, text, reply_markup=kb)
        logger.info("Morning protocol sent to user=%s", user_id)
    except Exception:
        logger.exception("Failed to send morning protocol")
