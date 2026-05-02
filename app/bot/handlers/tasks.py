import asyncio
import logging
from datetime import datetime
from typing import Protocol

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.domain.task_service import TaskService
from app.parsing.task_engine import ParseResult, parse_date_input, parse_task, parse_time_input
from app.storage.db import SessionLocal
from app.storage.task_repo import TaskRepo


logger = logging.getLogger(__name__)

tasks_router = Router(name="tasks")


class EditStates(StatesGroup):
    date = State()
    time = State()


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
    lines = [
        "📝 Задача",
        f"Название: {task.text}",
        f"Дата: {_format_date(task.suggested_date)}",
        f"Время: {time_line}",
    ]
    if parser:
        lines.append(f"Парсер: {parser}")
    lines += ["", "Добавить в календарь?"]
    return "\n".join(lines)


def _build_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_{task_id}"),
            InlineKeyboardButton(text="✏️ Изменить дату", callback_data=f"editdate_{task_id}"),
        ],
        [
            InlineKeyboardButton(text="🕐 Изменить время", callback_data=f"edittime_{task_id}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{task_id}"),
        ],
    ])


# ---------------------------------------------------------------------------
# Приём текстового сообщения — только когда нет активного FSM-состояния
# ---------------------------------------------------------------------------

@tasks_router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def handle_text(message: Message) -> None:
    raw = message.text.strip()
    user_id = message.from_user.id if message.from_user else 0

    try:
        parsed: ParseResult = await asyncio.to_thread(parse_task, raw)
    except ValueError as e:
        await message.answer(f"⚠️ {e}")
        return
    except Exception:
        logger.exception("parse_task failed for %r", raw)
        await message.answer("⚠️ Не смог разобрать задачу. Попробуй: завтра в 15:00 встреча")
        return

    with SessionLocal() as session:
        svc = TaskService(session)
        task_id = svc.create_task(parsed, user_id)
        repo = TaskRepo(session)
        task = repo.get(task_id)

    if task is None:
        await message.answer("⚠️ Задача записана, но не удалось её прочитать.")
        return

    await message.answer(
        _build_card(task, parser=parsed.parser),
        reply_markup=_build_keyboard(task_id),
    )


# ---------------------------------------------------------------------------
# Callback: подтвердить
# ---------------------------------------------------------------------------

@tasks_router.callback_query(F.data.startswith("confirm_"))
async def cb_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    task_id = int(callback.data.split("_", 1)[1])

    with SessionLocal() as session:
        svc = TaskService(session)
        try:
            uid = svc.confirm_and_sync(task_id)
        except ValueError:
            await callback.message.edit_text("⚠️ Задача не найдена.")
            return

        repo = TaskRepo(session)
        task = repo.get(task_id)

    card = _build_card(task).replace("\nДобавить в календарь?", "")
    if uid:
        text = f"✅ Добавлено в календарь.\n\n{card}"
    else:
        text = f"✅ Подтверждено, но синхронизация календаря недоступна.\n\n{card}"

    await callback.message.edit_text(text)


# ---------------------------------------------------------------------------
# Callback: отменить задачу
# ---------------------------------------------------------------------------

@tasks_router.callback_query(F.data.startswith("cancel_"))
async def cb_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    task_id = int(callback.data.split("_", 1)[1])

    with SessionLocal() as session:
        repo = TaskRepo(session)
        deleted = repo.delete(task_id)
        session.commit()

    if deleted:
        await callback.message.edit_text("❌ Задача отменена.")
    else:
        await callback.message.edit_text("⚠️ Задача не найдена.")


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
        return  # остаёмся в EditStates.date
    except Exception:
        logger.exception("parse_date_input failed for %r", message.text)
        await message.answer("⚠️ Ошибка парсера. Попробуй ещё раз или /cancel.")
        return  # остаёмся в EditStates.date

    with SessionLocal() as session:
        repo = TaskRepo(session)
        ok = repo.update_date_time(task_id, parsed.date, parsed.time, parsed.all_day)
        session.commit()
        task = repo.get(task_id)

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
        return  # остаёмся в EditStates.time
    except Exception:
        logger.exception("parse_time_input failed for %r", message.text)
        await message.answer("⚠️ Ошибка парсера. Попробуй ещё раз или /cancel.")
        return  # остаёмся в EditStates.time

    with SessionLocal() as session:
        repo = TaskRepo(session)
        ok = repo.update_time(task_id, event_time, all_day)
        session.commit()
        task = repo.get(task_id)

    if not ok or task is None:
        await message.answer("⚠️ Не удалось обновить задачу.")
        await state.clear()
        return

    await state.clear()
    await message.answer(
        "✅ Время обновлено.\n\n" + _build_card(task),
        reply_markup=_build_keyboard(task_id),
    )
