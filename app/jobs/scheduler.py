import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from aiogram import Bot

from app.config import settings


logger = logging.getLogger(__name__)


def start_scheduler(bot: Bot) -> AsyncIOScheduler:
    from app.jobs.due_pings import check_due_items
    from app.jobs.morning_plan import send_morning_plan
    from app.jobs.evening_review import send_evening_review

    scheduler = AsyncIOScheduler(timezone=settings.task_timezone)

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

    scheduler.start()
    logger.info(
        "Scheduler started (tz=%s): check_due_items/10min, morning_plan/08:00, evening_review/21:00",
        settings.task_timezone,
    )
    return scheduler
