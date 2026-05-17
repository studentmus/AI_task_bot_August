"""Daily recurring tasks spawn at 07:00.

For each active RecurringTask that fires today, creates a one-time Task
(if one with the same text doesn't already exist for today).
"""

import logging
from datetime import date

from aiogram import Bot

from app.config import settings
from app.domain.task_service import TaskService
from app.storage.db import SessionLocal, Task
from app.storage.recurring_repo import RecurringRepo
from app.storage.task_repo import TaskRepo

logger = logging.getLogger(__name__)


async def spawn_recurring_tasks(bot: Bot) -> None:
    user_id = settings.allowed_user_id
    if not user_id:
        return

    try:
        today = date.today()
        today_str = today.isoformat()
        weekday = today.weekday()  # 0=Mon … 6=Sun

        with SessionLocal() as session:
            repo = RecurringRepo(session)
            task_repo = TaskRepo(session)

            due = repo.get_all_due_today(today_str, weekday)
            created: list[str] = []

            for rt in due:
                # Skip if a task with this text already exists for today
                existing = (
                    session.query(Task)
                    .filter_by(
                        telegram_user_id=rt.telegram_user_id,
                        suggested_date=today_str,
                        text=rt.text,
                    )
                    .first()
                )
                if existing:
                    continue

                task_id = task_repo.insert_pending(
                    text=rt.text,
                    date=today_str,
                    event_time=rt.event_time,
                    all_day=(rt.event_time is None),
                    user_id=rt.telegram_user_id,
                )
                # Auto-confirm so it shows in today's plan
                TaskService(session).confirm_and_sync(task_id)
                created.append(rt.text)

            session.commit()

        if created and user_id:
            lines = "\n".join(f"• {t}" for t in created)
            await bot.send_message(
                user_id,
                f"🔁 Повторяющиеся задачи на сегодня:\n{lines}",
            )
            logger.info("Spawned %d recurring task(s): %s", len(created), created)

    except Exception:
        logger.exception("spawn_recurring_tasks failed")
