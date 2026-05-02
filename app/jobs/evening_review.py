import logging
from datetime import date

from aiogram import Bot

from app.config import settings
from app.storage.db import SessionLocal, Task


logger = logging.getLogger(__name__)


async def send_evening_review(bot: Bot) -> None:
    user_id = settings.allowed_user_id
    if user_id is None:
        logger.warning("evening_review: allowed_user_id not set, skipping")
        return

    today_str = date.today().isoformat()

    with SessionLocal() as session:
        done_tasks = (
            session.query(Task)
            .filter(
                Task.telegram_user_id == user_id,
                Task.suggested_date == today_str,
                Task.status == "done",
            )
            .all()
        )
        pending_tasks = (
            session.query(Task)
            .filter(
                Task.telegram_user_id == user_id,
                Task.suggested_date == today_str,
                Task.status.in_(["pending", "confirmed"]),
            )
            .order_by(Task.event_time.asc().nulls_last())
            .all()
        )

    lines = ["🌙 Итоги дня:\n"]

    if done_tasks:
        lines.append(f"✅ Выполнено ({len(done_tasks)}):")
        for t in done_tasks:
            lines.append(f"  • {t.text}")

    if pending_tasks:
        lines.append(f"\n⏳ Не выполнено ({len(pending_tasks)}):")
        for t in pending_tasks:
            time_part = f" [{t.event_time}]" if (not t.all_day and t.event_time) else ""
            lines.append(f"  • {t.text}{time_part}")

    if not done_tasks and not pending_tasks:
        lines.append("Сегодня задач не было. Хороший отдых! 🛋")

    await bot.send_message(user_id, "\n".join(lines))
    logger.info(
        "Evening review sent to user=%s done=%d pending=%d",
        user_id, len(done_tasks), len(pending_tasks),
    )
