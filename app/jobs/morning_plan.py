import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.config import settings
from app.storage.db import SessionLocal
from app.storage.task_repo import TaskRepo


logger = logging.getLogger(__name__)


def _format_task_line(task, index: int) -> str:
    time_part = task.event_time if (not task.all_day and task.event_time) else "весь день"
    return f"{index}. {time_part} — {task.text}"


async def send_morning_plan(bot: Bot) -> None:
    user_id = settings.allowed_user_id
    if user_id is None:
        logger.warning("morning_plan: allowed_user_id not set, skipping")
        return

    today_str = datetime.now(ZoneInfo(settings.task_timezone)).date().isoformat()

    with SessionLocal() as session:
        repo = TaskRepo(session)
        tasks = repo.get_today_plan(user_id, today=today_str)

    if not tasks:
        await bot.send_message(user_id, "☀️ Доброе утро! На сегодня задач нет.")
        return

    lines = ["☀️ Доброе утро! План на сегодня:\n"]
    for i, task in enumerate(tasks, start=1):
        lines.append(_format_task_line(task, i))

    await bot.send_message(user_id, "\n".join(lines))
    logger.info("Morning plan sent to user=%s tasks=%d", user_id, len(tasks))
