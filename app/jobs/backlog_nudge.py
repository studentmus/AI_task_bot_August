"""Daily backlog nudge at 14:00 — pick one undated task and suggest it."""

import logging
import random

from aiogram import Bot

from app.config import settings
from app.storage.db import SessionLocal
from app.storage.task_repo import TaskRepo

logger = logging.getLogger(__name__)


async def send_backlog_nudge(bot: Bot) -> None:
    user_id = settings.allowed_user_id
    if not user_id:
        return

    try:
        with SessionLocal() as session:
            backlog = TaskRepo(session).get_backlog_tasks(user_id, limit=10)

        if not backlog:
            return

        task = random.choice(backlog[:5])  # pick from top-5 oldest
        text = (
            f"📥 В бэклоге лежит: <b>{task.text}</b>\n"
            f"Сейчас 14:00 — хорошее время чтобы разобраться с этим. "
            f"Хочешь назначить время или выполнить?"
        )
        await bot.send_message(user_id, text)
        logger.info("Backlog nudge sent: task id=%s", task.id)
    except Exception:
        logger.exception("send_backlog_nudge failed")
