import logging

from aiogram import Bot
from aiogram.fsm.storage.base import BaseStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings

logger = logging.getLogger(__name__)


def start_scheduler(bot: Bot, storage: BaseStorage) -> AsyncIOScheduler:
    from app.jobs.due_pings import check_due_items
    from app.jobs.morning_plan import send_morning_plan
    from app.jobs.evening_review import send_evening_review
    from app.jobs.ping_service import send_scheduled_ping

    scheduler = AsyncIOScheduler(timezone=settings.task_timezone)

    # ── Existing jobs ────────────────────────────────────────────────────────
    scheduler.add_job(
        check_due_items,
        trigger=IntervalTrigger(minutes=10),
        args=[bot],
        id="check_due_items",
        replace_existing=True,
    )
    scheduler.add_job(
        send_morning_plan,
        trigger=CronTrigger(hour=8, minute=0, timezone=settings.task_timezone),
        args=[bot],
        id="morning_plan",
        replace_existing=True,
    )
    scheduler.add_job(
        send_evening_review,
        trigger=CronTrigger(hour=21, minute=0, timezone=settings.task_timezone),
        args=[bot],
        id="evening_review",
        replace_existing=True,
    )

    # ── Scheduled pings ──────────────────────────────────────────────────────
    # Template: copy this block and change hour/minute, question, filename, id.
    # filename must match an existing file in {OBSIDIAN_VAULT_PATH}/_bot/.
    scheduler.add_job(
        send_scheduled_ping,
        trigger=CronTrigger(hour=22, minute=0, timezone=settings.task_timezone),
        kwargs={"bot": bot, "storage": storage, "question": "Как самочувствие сегодня? Что было с тренировкой?", "filename": "health.md"},
        id="ping_health",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler started (tz=%s): check_due/10min, morning/08:00, evening/21:00, ping_health/22:00",
        settings.task_timezone,
    )
    return scheduler
