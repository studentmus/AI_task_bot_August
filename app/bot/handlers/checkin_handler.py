import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.config import settings
from app.storage.db import SessionLocal
from app.storage.task_repo import TaskRepo

logger = logging.getLogger(__name__)
checkin_router = Router(name="checkin")


@checkin_router.callback_query(F.data.startswith("sleep_rate:"))
async def cb_sleep_rate(callback: CallbackQuery) -> None:
    await callback.answer()
    score = int(callback.data.split(":")[1])

    tz = ZoneInfo(settings.task_timezone)
    ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    entry = f"- [{ts}] Качество сна: {score}/10"

    try:
        from app.llm.obsidian_tools import append_to_bot_log
        await append_to_bot_log("sleep.md", entry)
    except Exception:
        logger.exception("Failed to write sleep quality to sleep.md")

    if callback.message:
        await callback.message.edit_text(f"✅ Качество сна записано: {score}/10")


@checkin_router.callback_query(F.data == "idle:plan")
async def cb_idle_plan(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    user_id = callback.from_user.id if callback.from_user else settings.allowed_user_id

    today_str = date.today().isoformat()
    with SessionLocal() as session:
        tasks = TaskRepo(session).get_today_plan(user_id, today=today_str)

    if not tasks:
        await callback.message.edit_text("📋 На сегодня задач нет.")
        return

    lines = ["📋 Сегодня:"]
    for t in tasks:
        time_str = f"{t.event_time.split('-')[0]} — " if (t.event_time and not t.all_day) else ""
        lines.append(f"• {time_str}{t.text}")
    await callback.message.edit_text("\n".join(lines))


@checkin_router.callback_query(F.data.startswith("idle:rest:"))
async def cb_idle_rest(callback: CallbackQuery) -> None:
    await callback.answer()
    minutes = int(callback.data.split(":")[2])

    from app.jobs.idle_detector import snooze_idle
    snooze_idle(minutes)

    if callback.message:
        await callback.message.edit_text(f"😴 Окей, отдыхай {minutes} мин. Напомню потом.")


@checkin_router.callback_query(F.data == "idle:busy")
async def cb_idle_busy(callback: CallbackQuery) -> None:
    await callback.answer()
    from app.jobs.idle_detector import snooze_idle
    snooze_idle(90)
    if callback.message:
        await callback.message.edit_text("✅ Окей, продолжай!")


@checkin_router.callback_query(F.data == "evening:plan")
async def cb_evening_plan(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "📝 Напиши что планируешь на завтра — добавлю в задачи.\n\n"
            "Например: «завтра в 10:00 позвонить в банк»"
        )


@checkin_router.callback_query(F.data == "evening:skip")
async def cb_evening_skip(callback: CallbackQuery) -> None:
    await callback.answer("Окей!")
    if callback.message:
        await callback.message.edit_text("⏩ Пропущено.")


# ---------------------------------------------------------------------------
# Утренний протокол
# ---------------------------------------------------------------------------

@checkin_router.callback_query(F.data.startswith("proto:"))
async def cb_proto_item(callback: CallbackQuery) -> None:
    await callback.answer()
    parts = callback.data.split(":")
    if len(parts) < 3:
        return
    action = parts[1]   # "done" or "skip"
    item_id = parts[2]

    from app.jobs.morning_protocol import build_protocol_message, set_item_status
    set_item_status(item_id, action)
    text, kb = build_protocol_message()

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
