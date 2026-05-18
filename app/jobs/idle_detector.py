import logging
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings

logger = logging.getLogger(__name__)

_IDLE_THRESHOLD_MIN = 45
_PING_COOLDOWN_MIN = 120

_last_task_done_at: Optional[datetime] = None
_last_interaction_at: Optional[datetime] = None
_idle_ping_sent_at: Optional[datetime] = None


def mark_task_done() -> None:
    global _last_task_done_at, _idle_ping_sent_at
    _last_task_done_at = datetime.now()
    _idle_ping_sent_at = None  # reset so next idle can fire


def mark_interaction() -> None:
    global _last_interaction_at
    _last_interaction_at = datetime.now()


def snooze_idle(minutes: int) -> None:
    """Delay next idle ping by N minutes (user chose to rest or is busy)."""
    global _idle_ping_sent_at
    # Shift sent_at back so next check fires after `minutes` from now
    _idle_ping_sent_at = datetime.now() - timedelta(minutes=_PING_COOLDOWN_MIN - minutes)


async def check_idle(bot: Bot) -> None:
    user_id = settings.allowed_user_id
    if not user_id or _last_task_done_at is None:
        return

    now = datetime.now()
    since_done = (now - _last_task_done_at).total_seconds() / 60
    if since_done < _IDLE_THRESHOLD_MIN:
        return

    if _last_interaction_at:
        since_interaction = (now - _last_interaction_at).total_seconds() / 60
        if since_interaction < _IDLE_THRESHOLD_MIN:
            return

    if _idle_ping_sent_at:
        since_ping = (now - _idle_ping_sent_at).total_seconds() / 60
        if since_ping < _PING_COOLDOWN_MIN:
            return

    # Don't ping if there are confirmed tasks in the next 2 hours
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(settings.task_timezone)
    now_tz = datetime.now(tz)
    today_str = now_tz.date().isoformat()
    now_time = now_tz.strftime("%H:%M")
    in_2h_time = (now_tz + timedelta(hours=2)).strftime("%H:%M")

    from app.storage.db import SessionLocal
    from app.storage.task_repo import TaskRepo
    with SessionLocal() as session:
        tasks = TaskRepo(session).get_today_plan(user_id, today=today_str)
        has_upcoming = any(
            t.event_time and now_time < t.event_time.split("-")[0].strip() <= in_2h_time
            for t in tasks
        )
        if has_upcoming:
            return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Показать план", callback_data="idle:plan"),
        InlineKeyboardButton(text="😴 Отдых 30м",     callback_data="idle:rest:30"),
        InlineKeyboardButton(text="✅ Уже занят",      callback_data="idle:busy"),
    ]])
    try:
        await bot.send_message(user_id, "⏱ 45 минут без задачи. Что дальше?", reply_markup=kb)
        global _idle_ping_sent_at
        _idle_ping_sent_at = now
        logger.info("Idle ping sent user=%s since_done=%.0fm", user_id, since_done)
    except Exception:
        logger.exception("Failed to send idle ping")
