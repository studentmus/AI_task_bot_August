"""Midday push: send one concrete next-step suggestion at 12:30."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.config import settings
from app.storage.db import SessionLocal

logger = logging.getLogger(__name__)


async def send_next_step_push(bot: Bot) -> None:
    user_id = settings.allowed_user_id
    if user_id is None:
        return

    tz = ZoneInfo(settings.task_timezone)
    today = datetime.now(tz=tz).strftime("%Y-%m-%d")

    # Quick pre-check: skip if no tasks and no alerts (quiet day)
    with SessionLocal() as session:
        from app.domain.alert_rules import run_all_checks
        from app.storage.task_repo import TaskRepo

        tasks = TaskRepo(session).get_today_plan(user_id, today=today)
        alerts = [a for a in run_all_checks(session, user_id) if a.severity == "warning"]

    if not tasks and not alerts:
        logger.debug("next_step_push: quiet day, skipping")
        return

    with SessionLocal() as session:
        from app.domain.next_step import suggest_next_step
        text = await suggest_next_step(session, user_id)

    await bot.send_message(user_id, f"🎯 {text}")
    logger.info("Next-step push sent to user=%s", user_id)
