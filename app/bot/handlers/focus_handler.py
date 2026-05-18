import logging
from datetime import datetime, timedelta
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import settings
from app.storage.db import SessionLocal
from app.storage.task_repo import TaskRepo

logger = logging.getLogger(__name__)
focus_router = Router(name="focus")

CYCLE_MINUTES = 25


class FocusStates(StatesGroup):
    waiting_text = State()


# ---------------------------------------------------------------------------
# In-memory focus session (single-user bot — fine on restart reset)
# ---------------------------------------------------------------------------

_focus: dict = {
    "active": False,
    "task_id": None,
    "task_text": "",
    "cycle": 0,
    "user_id": None,
    "paused": False,
}


def _is_active() -> bool:
    return bool(_focus.get("active"))


def _end_focus() -> None:
    _focus.update(active=False, task_id=None, task_text="", cycle=0, paused=False)
    # Cancel scheduled ping if any
    try:
        from app.jobs.scheduler_ref import get_scheduler
        s = get_scheduler()
        if s and s.get_job("focus_ping"):
            s.remove_job("focus_ping")
    except Exception:
        pass


def _schedule_ping(bot, user_id: int, minutes: int = CYCLE_MINUTES) -> None:
    try:
        from apscheduler.triggers.date import DateTrigger
        from app.jobs.scheduler_ref import get_scheduler
        s = get_scheduler()
        if s is None:
            return
        run_at = datetime.now() + timedelta(minutes=minutes)
        s.add_job(
            _send_focus_ping,
            trigger=DateTrigger(run_date=run_at),
            args=[bot, user_id],
            id="focus_ping",
            replace_existing=True,
        )
        logger.info("Focus ping scheduled in %d min", minutes)
    except Exception:
        logger.exception("Failed to schedule focus ping")


async def _send_focus_ping(bot, user_id: int) -> None:
    if not _focus["active"] or _focus["paused"]:
        return
    _focus["cycle"] += 1
    cycle = _focus["cycle"]
    task_text = _focus["task_text"]
    total_min = cycle * CYCLE_MINUTES

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"+{CYCLE_MINUTES} мин", callback_data="focus:extend"),
            InlineKeyboardButton(text="✅ Готово",              callback_data="focus:done"),
        ],
        [
            InlineKeyboardButton(text="⏸ Пауза 10м",  callback_data="focus:pause:10"),
            InlineKeyboardButton(text="🛑 Завершить",  callback_data="focus:stop"),
        ],
    ])
    try:
        await bot.send_message(
            user_id,
            f"⏱ <b>{total_min} мин в фокусе!</b>\n«{task_text}»\n\nКак прогресс?",
            reply_markup=kb,
        )
    except Exception:
        logger.exception("Failed to send focus ping")


async def _start_focus(message: Message, task_id: Optional[int], task_text: str) -> None:
    user_id = message.from_user.id if message.from_user else settings.allowed_user_id
    _focus.update(active=True, task_id=task_id, task_text=task_text, cycle=0,
                  user_id=user_id, paused=False)

    from aiogram import Bot
    bot: Bot = message.bot  # type: ignore[assignment]
    _schedule_ping(bot, user_id, CYCLE_MINUTES)

    await message.answer(
        f"🎯 <b>Фокус начат!</b>\n«{task_text}»\n\nЧерез {CYCLE_MINUTES} мин напомню о чекпоинте.\n"
        f"Чтобы остановить: /stopfocus",
    )
    logger.info("Focus started task_id=%s text=%r", task_id, task_text)


# ---------------------------------------------------------------------------
# /focus command
# ---------------------------------------------------------------------------

@focus_router.message(Command("focus"))
async def cmd_focus(message: Message) -> None:
    raw = (message.text or "").strip()
    # Extract argument after /focus
    parts = raw.split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if arg:
        await _start_focus(message, task_id=None, task_text=arg)
        return

    # No argument → show today's tasks as selector
    user_id = message.from_user.id if message.from_user else settings.allowed_user_id
    with SessionLocal() as session:
        tasks = TaskRepo(session).get_today_plan(user_id)

    if not tasks:
        await message.answer(
            "📝 На сегодня задач нет.\n\nНапиши: <code>/focus название задачи</code>"
        )
        return

    rows = [
        [InlineKeyboardButton(
            text=f"{'🔴 ' if (t.urgency or 0)*(t.importance or 0) >= 15 else ''}{'🟡 ' if 8 <= (t.urgency or 0)*(t.importance or 0) < 15 else ''}{t.text[:40]}",
            callback_data=f"focus_select:{t.id}",
        )]
        for t in tasks[:5]
    ]
    rows.append([InlineKeyboardButton(text="✏️ Другое", callback_data="focus_select:custom")])

    await message.answer(
        "🎯 Над чем работаешь?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@focus_router.message(Command("stopfocus"))
async def cmd_stopfocus(message: Message) -> None:
    if not _is_active():
        await message.answer("Нет активной фокус-сессии.")
        return
    task_text = _focus["task_text"]
    _end_focus()
    await message.answer(f"🛑 Фокус «{task_text}» завершён.")


# ---------------------------------------------------------------------------
# Task selector callbacks
# ---------------------------------------------------------------------------

@focus_router.callback_query(F.data.startswith("focus_select:"))
async def cb_focus_select(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    value = callback.data.split(":", 1)[1]

    if value == "custom":
        await state.set_state(FocusStates.waiting_text)
        if callback.message:
            await callback.message.edit_text("✏️ Напиши задачу:")
        return

    task_id = int(value)
    with SessionLocal() as session:
        task = TaskRepo(session).get(task_id)

    if task is None:
        if callback.message:
            await callback.message.edit_text("⚠️ Задача не найдена.")
        return

    if callback.message:
        await callback.message.delete()
    await _start_focus(callback.message, task_id=task_id, task_text=task.text)


@focus_router.message(FocusStates.waiting_text, F.text)
async def fsm_focus_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip() if message.text else ""
    if not text:
        await message.answer("Текст не может быть пустым.")
        return
    await state.clear()
    await _start_focus(message, task_id=None, task_text=text)


# ---------------------------------------------------------------------------
# Focus session callbacks
# ---------------------------------------------------------------------------

@focus_router.callback_query(F.data == "focus:extend")
async def cb_focus_extend(callback: CallbackQuery) -> None:
    await callback.answer(f"+{CYCLE_MINUTES} мин!")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
    user_id = callback.from_user.id if callback.from_user else settings.allowed_user_id
    _schedule_ping(callback.message.bot, user_id, CYCLE_MINUTES)


@focus_router.callback_query(F.data == "focus:done")
async def cb_focus_done(callback: CallbackQuery) -> None:
    await callback.answer()
    task_id = _focus.get("task_id")
    task_text = _focus.get("task_text", "")
    cycle = _focus.get("cycle", 0)
    _end_focus()

    result_line = f"✅ «{task_text}» — выполнено! {cycle * CYCLE_MINUTES} мин в фокусе."

    if task_id:
        try:
            with SessionLocal() as session:
                from app.domain.task_actions import TaskActions
                result = TaskActions(session).complete_task(task_id)
            result_line = f"{result}\n🕐 {cycle * CYCLE_MINUTES} мин в фокусе."
        except Exception:
            logger.exception("complete_task failed for task_id=%s", task_id)

    if callback.message:
        await callback.message.edit_text(result_line)


@focus_router.callback_query(F.data.startswith("focus:pause:"))
async def cb_focus_pause(callback: CallbackQuery) -> None:
    await callback.answer()
    minutes = int(callback.data.split(":")[2])
    _focus["paused"] = True
    task_text = _focus.get("task_text", "")

    user_id = callback.from_user.id if callback.from_user else settings.allowed_user_id

    resume_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Продолжить", callback_data="focus:resume"),
        InlineKeyboardButton(text="🛑 Завершить",  callback_data="focus:stop"),
    ]])

    async def _send_resume(bot, uid: int) -> None:
        if _focus.get("active") and _focus.get("paused"):
            _focus["paused"] = False
            await bot.send_message(
                uid,
                f"▶️ Пауза закончилась!\n«{task_text}» — продолжаем?",
                reply_markup=resume_kb,
            )

    # Schedule resume ping
    try:
        from apscheduler.triggers.date import DateTrigger
        from app.jobs.scheduler_ref import get_scheduler
        s = get_scheduler()
        if s:
            run_at = datetime.now() + timedelta(minutes=minutes)
            s.add_job(
                _send_resume,
                trigger=DateTrigger(run_date=run_at),
                args=[callback.message.bot, user_id],
                id="focus_pause_end",
                replace_existing=True,
            )
    except Exception:
        logger.exception("Failed to schedule pause end")

    if callback.message:
        await callback.message.edit_text(f"⏸ Пауза {minutes} мин. Отдыхай!\n«{task_text}»")


@focus_router.callback_query(F.data == "focus:resume")
async def cb_focus_resume(callback: CallbackQuery) -> None:
    await callback.answer()
    if not _is_active():
        if callback.message:
            await callback.message.edit_text("Сессия уже завершена.")
        return
    _focus["paused"] = False
    user_id = callback.from_user.id if callback.from_user else settings.allowed_user_id
    task_text = _focus.get("task_text", "")
    _schedule_ping(callback.message.bot, user_id, CYCLE_MINUTES)
    if callback.message:
        await callback.message.edit_text(
            f"▶️ Продолжаем! «{task_text}»\nЧерез {CYCLE_MINUTES} мин — чекпоинт."
        )


@focus_router.callback_query(F.data == "focus:stop")
async def cb_focus_stop(callback: CallbackQuery) -> None:
    await callback.answer()
    task_text = _focus.get("task_text", "")
    cycle = _focus.get("cycle", 0)
    _end_focus()
    if callback.message:
        total = cycle * CYCLE_MINUTES
        await callback.message.edit_text(
            f"🛑 Фокус завершён.\n«{task_text}» — {total} мин в работе."
        )


# ---------------------------------------------------------------------------
# "Focus" button on task card (callback_data = "focus_task:{task_id}")
# ---------------------------------------------------------------------------

@focus_router.callback_query(F.data.startswith("focus_task:"))
async def cb_focus_task(callback: CallbackQuery) -> None:
    await callback.answer()
    task_id = int(callback.data.split(":")[1])
    with SessionLocal() as session:
        task = TaskRepo(session).get(task_id)
    if task is None:
        if callback.message:
            await callback.message.edit_text("⚠️ Задача не найдена.")
        return
    if callback.message:
        await _start_focus(callback.message, task_id=task_id, task_text=task.text)
