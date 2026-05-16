import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.config import settings
from app.storage.db import SessionLocal
from app.storage.task_repo import TaskRepo


logger = logging.getLogger(__name__)

_BIG_TIME = "99:99"  # sentinel for all-day items (sort last)


def _task_sort_key(task) -> str:
    if task.all_day or not task.event_time:
        return _BIG_TIME
    return task.event_time.split("-")[0].strip()  # "15:00-16:00" → "15:00"


def _event_sort_key(ev) -> str:
    return _BIG_TIME if ev.all_day else ev.start_time


def _build_plan_text(today_str: str, tasks, cal_events) -> str:
    """Merge tasks and calendar events into one time-sorted message."""
    lines = ["☀️ Доброе утро! Сегодня:\n"]

    # Collect all items as (sort_key, text)
    items: list[tuple[str, str]] = []

    for task in tasks:
        key = _task_sort_key(task)
        time_part = task.event_time if (not task.all_day and task.event_time) else "весь день"
        items.append((key, f"✅ {time_part} — {task.text}"))

    for ev in cal_events:
        if ev.is_bot_task:
            continue  # already shown as a task above
        key = _event_sort_key(ev)
        if ev.all_day:
            time_part = "весь день"
        else:
            time_part = ev.start_time
            if ev.end_time:
                time_part += f"–{ev.end_time}"
        items.append((key, f"📅 {time_part} — {ev.summary}"))

    if not items:
        return "☀️ Доброе утро! На сегодня ничего нет."

    items.sort(key=lambda x: x[0])
    for _, text in items:
        lines.append(f"  {text}")

    total_tasks = len(tasks)
    total_events = sum(1 for e in cal_events if not e.is_bot_task)
    if total_events:
        lines.append(f"\n({total_tasks} задач · {total_events} событий в календаре)")

    return "\n".join(lines)


async def send_morning_plan(bot: Bot) -> None:
    user_id = settings.allowed_user_id
    if user_id is None:
        logger.warning("morning_plan: allowed_user_id not set, skipping")
        return

    today_str = datetime.now(ZoneInfo(settings.task_timezone)).date().isoformat()

    with SessionLocal() as session:
        tasks = TaskRepo(session).get_today_plan(user_id, today=today_str)

    # Fetch GCal events in thread (sync API) — failures are silent
    cal_events = []
    try:
        from app.domain.google_calendar import get_upcoming_events
        cal_events = await asyncio.to_thread(get_upcoming_events, today_str, 1)
    except Exception as exc:
        logger.warning("morning_plan: GCal fetch failed: %s", exc)

    if not tasks and not [e for e in cal_events if not e.is_bot_task]:
        await bot.send_message(user_id, "☀️ Доброе утро! На сегодня ничего нет.")
        return

    text = _build_plan_text(today_str, tasks, cal_events)
    await bot.send_message(user_id, text)
    logger.info(
        "Morning plan sent to user=%s tasks=%d gcal_events=%d",
        user_id, len(tasks), len(cal_events),
    )
