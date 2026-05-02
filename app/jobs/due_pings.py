import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.storage.db import SessionLocal, Task


logger = logging.getLogger(__name__)

PING_COOLDOWN_HOURS = 2


def _is_due(task: Task, today_str: str, now_time: str) -> bool:
    """Проверяет, нужно ли напомнить о задаче прямо сейчас."""
    if task.suggested_date is None:
        return False
    if task.suggested_date > today_str:
        return False
    if task.suggested_date < today_str:
        return True  # прошедшая дата — всегда просрочено
    # task.suggested_date == today
    if task.all_day or not task.event_time:
        return True  # задача на весь день — напоминаем в любое время
    return task.event_time <= now_time


def _is_cooldown_ok(task: Task, now: datetime) -> bool:
    """True если с последнего пинга прошло достаточно времени."""
    if not task.last_ping_at:
        return True
    try:
        last = datetime.fromisoformat(task.last_ping_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=ZoneInfo(settings.task_timezone))
        elapsed_hours = (now - last).total_seconds() / 3600
        return elapsed_hours >= PING_COOLDOWN_HOURS
    except Exception:
        return True


def _ping_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Сделал", callback_data=f"done_{task_id}"),
            InlineKeyboardButton(text="⏩ Позже", callback_data=f"snooze_{task_id}"),
        ],
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_{task_id}"),
        ],
    ])


def _ping_text(task: Task) -> str:
    time_part = ""
    if not task.all_day and task.event_time:
        time_part = f" в {task.event_time}"
    overdue = ""
    if task.suggested_date:
        from datetime import date
        today = date.today().isoformat()
        if task.suggested_date < today:
            overdue = " (просрочено)"

    lines = [
        f"🔔 Напоминание{overdue}",
        f"{task.text}",
        f"📅 {task.suggested_date}{time_part}",
    ]
    if task.ping_count and task.ping_count > 0:
        lines.append(f"Напоминание #{task.ping_count + 1}")
    return "\n".join(lines)


async def check_due_items(bot: Bot) -> None:
    tz = ZoneInfo(settings.task_timezone)
    now = datetime.now(tz)
    today_str = now.date().isoformat()
    now_time = now.strftime("%H:%M")

    with SessionLocal() as session:
        tasks = (
            session.query(Task)
            .filter(
                Task.status == "confirmed",
                Task.telegram_user_id.isnot(None),
                Task.suggested_date <= today_str,
            )
            .all()
        )

        due = [
            t for t in tasks
            if _is_due(t, today_str, now_time) and _is_cooldown_ok(t, now)
        ]

        if not due:
            return

        logger.info("Due items to ping: %d", len(due))

        for task in due:
            try:
                await bot.send_message(
                    chat_id=task.telegram_user_id,
                    text=_ping_text(task),
                    reply_markup=_ping_keyboard(task.id),
                )
                task.last_ping_at = now.isoformat(timespec="seconds")
                task.ping_count = (task.ping_count or 0) + 1
                logger.info("Pinged task id=%s ping_count=%s", task.id, task.ping_count)
            except Exception:
                logger.exception("Failed to ping task id=%s user=%s", task.id, task.telegram_user_id)

        session.commit()
