import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Protocol

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.domain.task_actions import TaskActions
from app.domain.task_service import TaskService
from app.parsing.task_engine import ParseResult, parse_date_input, parse_time_input
from app.storage.db import SessionLocal
from app.storage.task_repo import TaskRepo


logger = logging.getLogger(__name__)

tasks_router = Router(name="tasks")


class EditStates(StatesGroup):
    date  = State()
    time  = State()
    title = State()


class _TaskLike(Protocol):
    text: str
    suggested_date: str
    event_time: str | None
    all_day: bool


# ---------------------------------------------------------------------------
# Formatting helpers (временно здесь, переедут в formatters.py)
# ---------------------------------------------------------------------------

def _format_date(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        return f"{dt.strftime('%d.%m.%Y')} ({weekdays[dt.weekday()]})"
    except Exception:
        return date_str


def _build_card(task: _TaskLike, parser: str | None = None) -> str:
    time_line = "весь день" if task.all_day or not task.event_time else task.event_time
    return "\n".join([
        f"📝 <b>{task.text}</b>",
        f"📅 {_format_date(task.suggested_date)}",
        f"🕐 {time_line}",
    ])


def _build_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Дата",     callback_data=f"editdate_{task_id}"),
            InlineKeyboardButton(text="🕐 Время",    callback_data=f"edittime_{task_id}"),
        ],
        [
            InlineKeyboardButton(text="📝 Название", callback_data=f"edittitle_{task_id}"),
            InlineKeyboardButton(text="🗑 Удалить",  callback_data=f"cancel_{task_id}"),
        ],
    ])


# ---------------------------------------------------------------------------
# Callback: подтвердить (из карточки создания)
# ---------------------------------------------------------------------------

@tasks_router.callback_query(F.data.startswith("confirm_"))
async def cb_confirm(callback: CallbackQuery) -> None:
    """Обработчик старых сообщений с кнопкой ✅ — новые задачи подтверждаются автоматически."""
    await callback.answer()
    if callback.message is None:
        return
    task_id = int(callback.data.split("_", 1)[1])

    with SessionLocal() as session:
        svc = TaskService(session)
        try:
            svc.confirm_and_sync(task_id)
        except ValueError:
            await callback.message.edit_text("⚠️ Задача не найдена.")
            return
        task = TaskRepo(session).get(task_id)

    await callback.message.edit_text(
        _build_card(task),
        reply_markup=_build_keyboard(task_id),
    )


# ---------------------------------------------------------------------------
# Callback: отменить / удалить задачу (двухшаговое подтверждение)
# ---------------------------------------------------------------------------

@tasks_router.callback_query(F.data.startswith("cancel_confirm_"))
async def cb_cancel_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    task_id = int(callback.data.split("_", 2)[2])

    with SessionLocal() as session:
        repo = TaskRepo(session)
        task = repo.get(task_id)
        if task is None:
            await callback.message.edit_text("⚠️ Задача не найдена.")
            return
        task.status = "cancelled"
        session.commit()

    await callback.message.edit_text("Задача отменена.")


@tasks_router.callback_query(F.data.startswith("cancel_abort_"))
async def cb_cancel_abort(callback: CallbackQuery) -> None:
    await callback.answer("Отменено.")
    if callback.message is None:
        return
    task_id = int(callback.data.split("_", 2)[2])

    with SessionLocal() as session:
        task = TaskRepo(session).get(task_id)

    if task:
        try:
            await callback.message.edit_text(
                _build_card(task), reply_markup=_build_keyboard(task_id)
            )
        except Exception:
            pass
    else:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


@tasks_router.callback_query(F.data.startswith("cancel_"))
async def cb_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    task_id = int(callback.data.split("_", 1)[1])

    with SessionLocal() as session:
        task = TaskRepo(session).get(task_id)

    if task is None:
        await callback.message.edit_text("⚠️ Задача не найдена.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"cancel_confirm_{task_id}"),
        InlineKeyboardButton(text="❌ Нет",          callback_data=f"cancel_abort_{task_id}"),
    ]])
    await callback.message.edit_text(f"❓ Удалить «{task.text}»?", reply_markup=kb)


# ---------------------------------------------------------------------------
# Callback: выполнено (из пинга)
# ---------------------------------------------------------------------------

@tasks_router.callback_query(F.data.startswith("done_"))
async def cb_ping_done(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    task_id = int(callback.data.split("_", 1)[1])

    with SessionLocal() as session:
        actions = TaskActions(session)
        try:
            result = actions.complete_task(task_id)
        except ValueError as e:
            await callback.message.edit_text(f"⚠️ {e}")
            return

    await callback.message.edit_text(result)


# ---------------------------------------------------------------------------
# Callback: отложить на завтра (из пинга)
# ---------------------------------------------------------------------------

@tasks_router.callback_query(F.data.startswith("snooze_"))
async def cb_ping_snooze(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    task_id = int(callback.data.split("_", 1)[1])

    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    with SessionLocal() as session:
        repo = TaskRepo(session)
        task = repo.get(task_id)
        if task is None:
            await callback.message.edit_text("⚠️ Задача не найдена.")
            return
        until_time = task.event_time if not task.all_day else None

        actions = TaskActions(session)
        try:
            result = actions.snooze_task(task_id, tomorrow, until_time)
        except ValueError as e:
            await callback.message.edit_text(f"⚠️ {e}")
            return

    await callback.message.edit_text(result)


# ---------------------------------------------------------------------------
# Callback + FSM: изменить дату
# ---------------------------------------------------------------------------

@tasks_router.callback_query(F.data.startswith("editdate_"))
async def cb_edit_date_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message is None:
        return
    task_id = int(callback.data.split("_", 1)[1])
    await state.set_state(EditStates.date)
    await state.update_data(edit_task_id=task_id)
    await callback.message.edit_text(
        "✏️ Введи новую дату.\n\n"
        "Примеры:\n"
        "• завтра\n"
        "• 4 мая\n"
        "• в понедельник\n"
        "• в следующую пятницу\n"
        "• завтра в 15:00"
    )


@tasks_router.message(EditStates.date, F.text)
async def fsm_edit_date_receive(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id: int = data["edit_task_id"]

    try:
        parsed: ParseResult = await asyncio.to_thread(parse_date_input, message.text.strip())
    except ValueError:
        await message.answer(
            "⚠️ Не распознал дату. Попробуй: завтра, 4 мая, в пятницу, через 3 дня.\n"
            "Или /cancel для отмены."
        )
        return
    except Exception:
        logger.exception("parse_date_input failed for %r", message.text)
        await message.answer("⚠️ Ошибка парсера. Попробуй ещё раз или /cancel.")
        return

    with SessionLocal() as session:
        repo = TaskRepo(session)
        ok = repo.update_date_time(task_id, parsed.date, parsed.time, parsed.all_day)
        session.commit()
        task = repo.get(task_id)

        if task and task.google_event_id:
            try:
                from app.domain.google_calendar import update_event
                update_event(task.google_event_id, task)
            except Exception:
                logger.exception("Google Calendar update failed for task id=%s", task_id)

    if not ok or task is None:
        await message.answer("⚠️ Не удалось обновить задачу.")
        await state.clear()
        return

    await state.clear()
    await message.answer(
        "✅ Дата обновлена.\n\n" + _build_card(task),
        reply_markup=_build_keyboard(task_id),
    )


# ---------------------------------------------------------------------------
# Callback + FSM: изменить время
# ---------------------------------------------------------------------------

@tasks_router.callback_query(F.data.startswith("edittime_"))
async def cb_edit_time_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message is None:
        return
    task_id = int(callback.data.split("_", 1)[1])
    await state.set_state(EditStates.time)
    await state.update_data(edit_task_id=task_id)
    await callback.message.edit_text(
        "🕐 Введи новое время.\n\n"
        "Примеры:\n"
        "• 15:00\n"
        "• в 3 часа\n"
        "• утром / днём / вечером\n"
        "• без времени"
    )


@tasks_router.message(EditStates.time, F.text)
async def fsm_edit_time_receive(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id: int = data["edit_task_id"]

    try:
        event_time, all_day = await asyncio.to_thread(parse_time_input, message.text.strip())
    except ValueError:
        await message.answer(
            "⚠️ Не распознал время. Попробуй: 15:00, утром, вечером, без времени.\n"
            "Или /cancel для отмены."
        )
        return
    except Exception:
        logger.exception("parse_time_input failed for %r", message.text)
        await message.answer("⚠️ Ошибка парсера. Попробуй ещё раз или /cancel.")
        return

    with SessionLocal() as session:
        repo = TaskRepo(session)
        ok = repo.update_time(task_id, event_time, all_day)
        session.commit()
        task = repo.get(task_id)

        if task and task.google_event_id:
            try:
                from app.domain.google_calendar import update_event
                update_event(task.google_event_id, task)
            except Exception:
                logger.exception("Google Calendar update failed for task id=%s", task_id)

    if not ok or task is None:
        await message.answer("⚠️ Не удалось обновить задачу.")
        await state.clear()
        return

    await state.clear()
    await message.answer(
        "✅ Время обновлено.\n\n" + _build_card(task),
        reply_markup=_build_keyboard(task_id),
    )


# ---------------------------------------------------------------------------
# Callback + FSM: изменить название
# ---------------------------------------------------------------------------

@tasks_router.callback_query(F.data.startswith("edittitle_"))
async def cb_edit_title_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message is None:
        return
    task_id = int(callback.data.split("_", 1)[1])
    await state.set_state(EditStates.title)
    await state.update_data(edit_task_id=task_id)
    with SessionLocal() as session:
        task = TaskRepo(session).get(task_id)
    current = f" (сейчас: «{task.text}»)" if task else ""
    await callback.message.edit_text(f"📝 Введи новое название{current}:")


@tasks_router.message(EditStates.title, F.text)
async def fsm_edit_title_receive(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id: int = data["edit_task_id"]
    new_title = message.text.strip()

    if not new_title:
        await message.answer("⚠️ Название не может быть пустым.")
        return

    with SessionLocal() as session:
        from app.domain.task_actions import TaskActions
        actions = TaskActions(session)
        try:
            result = actions.edit_task_title(task_id, new_title)
        except ValueError as e:
            await message.answer(f"⚠️ {e}")
            await state.clear()
            return
        task = TaskRepo(session).get(task_id)

    await state.clear()
    await message.answer(
        result + "\n\n" + _build_card(task),
        reply_markup=_build_keyboard(task_id),
    )
