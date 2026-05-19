"""
FSM-based deterministic logging handler.

Flow:
  Button / slash cmd → FSM state set → any next text → direct file write (no LLM)
  Stop word or new button → exit / switch state
  Auto-exit after 60 seconds of inactivity.

Slash commands:
  /sleep [text]   — log sleep entry (flexible time parsing)
  /meal  [text]   — log nutrition entry
  /workout [text] — log training entry
  /german [text]  — log German vocabulary
  /ideas [text]   — log idea
  /ctx   [text]   — log personal context note
  /stop           — exit active log state
  /undo [sphere]  — delete last entry in active or named sphere
"""

import asyncio
import logging
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.bot.states import LogState
from app.config import settings
from app.llm.obsidian_tools import append_to_bot_log, delete_last_log_entry

log_router = Router(name="log")
logger = logging.getLogger(__name__)


# ── Reply keyboard (persistent, shown at /start) ────────────────────────────

LOG_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="📝 Записать →"),
            KeyboardButton(text="🎯 Что делать?"),
        ],
        [
            KeyboardButton(text="⚙️ Команды"),
            KeyboardButton(text="📋 Бэклог"),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

_LANG_BUTTON = "🌍 Языки"
_WRITE_BUTTON = "📝 Записать →"
_NEXT_BTN = "🎯 Что делать?"
_CMDS_BUTTON = "⚙️ Команды"
_BACKLOG_BUTTON = "📋 Бэклог"

# Маппинг callback-key → btn_text в _SPHERES
_KEY_TO_BTN: dict[str, str] = {
    "sleep":     "🌙 Сон",
    "nutrition": "🍽 Питание",
    "training":  "💪 Тренировка",
    "german":    "📖 Немецкий",
    "romanian":  "🇷🇴 Румынский",
    "ideas":     "💡 Идеи",
    "wishlist":  "🛒 Список",
    "context":   "📊 Контекст",
    "guitar":    "🎸 Гитара",
}

# ── Sphere config ────────────────────────────────────────────────────────────

_SPHERES: dict[str, dict] = {
    "🌙 Сон":        {"state": LogState.sleep,     "file": "sleep.md",        "label": "Сон"},
    "🍽 Питание":    {"state": LogState.nutrition,  "file": "nutrition.md",    "label": "Питание"},
    "💪 Тренировка": {"state": LogState.training,   "file": "training.md",     "label": "Тренировка"},
    "📖 Немецкий":   {"state": LogState.german,     "file": "german.md",       "label": "Немецкий"},
    "🇷🇴 Румынский": {"state": LogState.romanian,   "file": "romanian.md",     "label": "Румынский"},
    "💡 Идеи":       {"state": LogState.ideas,      "file": "ideas.md",        "label": "Идеи"},
    "📊 Контекст":   {"state": LogState.context,    "file": "ivan_context.md", "label": "Контекст"},
    "🛒 Список":     {"state": LogState.wishlist,   "file": "wishlist.md",     "label": "Список покупок"},
    "🎸 Гитара":     {"state": LogState.guitar,     "file": "guitar.md",       "label": "Гитара"},
}

# Кнопки reply-keyboard (не включают языки — они в подменю)
_BUTTON_TEXTS = {"🌙 Сон", "🍽 Питание", "💪 Тренировка", "💡 Идеи", "📊 Контекст"}

_STATE_PROMPTS = {
    "🌙 Сон":        "🌙 Режим: Сон.\nПиши время и заметки, например:\n  23:30–7:00\n  с полуночи до восьми\n  12–8, спал отлично",
    "🍽 Питание":    "🍽 Режим: Питание. Что ел/пил?",
    "💪 Тренировка": "💪 Режим: Тренировка. Что делал?",
    "📖 Немецкий":   "📖 Режим: Немецкий. Что учил?",
    "🇷🇴 Румынский": "🇷🇴 Режим: Румынский. Что учил?",
    "💡 Идеи":       "💡 Режим: Идеи. Пиши:",
    "📊 Контекст":   "📊 Режим: Контекст. Что зафиксировать?",
    "🛒 Список":     "🛒 Режим: Список покупок.\nПиши что хочешь купить, можно с категорией:\n  [Техника] AirPods Pro\n  [Быт] фильтр для воды\n  [Одежда] кроссовки Nike",
    "🎸 Гитара":     "🎸 Режим: Гитара. Что играл / учил?",
}

# ── Stop pattern ─────────────────────────────────────────────────────────────

_STOP_RE = re.compile(
    r"^(?:стоп|stop|выход|exit|готово|done|хватит|всё|все|назад|back|отмена|cancel)\s*$",
    re.IGNORECASE,
)

# ── Log escape pattern ────────────────────────────────────────────────────────
# Сообщения, которые явно НЕ являются лог-записями даже внутри LogState.
# При совпадении: выходим из FSM-режима, передаём в LLM как обычный запрос.
_LOG_ESCAPE_RE = re.compile(
    r"\?"                                                    # любой вопрос
    r"|\bзавтра\b|\bпослезавтра\b"                          # будущие события
    r"|\bв\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b"
    r"|\bчерез\s+\d+\s+(?:день|дня|дней|час|часа|часов|неделю|недели)\b"
    r"|\b(?:скинь|пришли|объясни|расскажи|помоги|найди|переведи|напомни)\b"
    r"|\bсоздай\s+задачу\b|\bпоставь\s+задачу\b|\bдобавь\s+в\s+календарь\b"
    r"|\bчто\s+(?:мне\s+)?(?:сейчас\s+)?(?:делать|сделать|нужно|дальше)\b"
    r"|\bследующий\s+шаг\b",
    re.IGNORECASE,
)

# ── Sphere arg → filename (for /undo <sphere>) ────────────────────────────────

_SPHERE_ARG_TO_FILE: dict[str, str] = {
    "sleep": "sleep.md",       "сон": "sleep.md",
    "meal": "nutrition.md",    "nutrition": "nutrition.md",   "питание": "nutrition.md",
    "workout": "training.md",  "training": "training.md",     "тренировка": "training.md",
    "german": "german.md",     "немецкий": "german.md",       "de": "german.md",
    "romanian": "romanian.md", "румынский": "romanian.md",    "ro": "romanian.md",
    "ideas": "ideas.md",       "идеи": "ideas.md",            "idea": "ideas.md",
    "ctx": "ivan_context.md",  "context": "ivan_context.md",  "контекст": "ivan_context.md",
    "health": "health.md",     "здоровье": "health.md",
    "wish": "wishlist.md",     "wishlist": "wishlist.md",     "список": "wishlist.md",
    "guitar": "guitar.md",     "гитара": "guitar.md",
}

# ── Auto-exit timeout ─────────────────────────────────────────────────────────

_TIMEOUT_SECS = 60
_timeout_tasks: dict[int, asyncio.Task] = {}


def _cancel_timeout(user_id: int) -> None:
    task = _timeout_tasks.pop(user_id, None)
    if task:
        task.cancel()


async def _auto_exit(bot, chat_id: int, user_id: int, state: FSMContext) -> None:
    await asyncio.sleep(_TIMEOUT_SECS)
    current = await state.get_state()
    if not (current and "LogState" in str(current)):
        return
    data = await state.get_data()
    label = data.get("label", "")
    await state.clear()
    _timeout_tasks.pop(user_id, None)
    note = f" ({label})" if label else ""
    try:
        await bot.send_message(chat_id, f"⏱ Режим{note} завершён — нет активности 1 минуту.")
    except Exception:
        pass


def _start_timeout(bot, chat_id: int, user_id: int, state: FSMContext) -> None:
    _cancel_timeout(user_id)
    _timeout_tasks[user_id] = asyncio.create_task(
        _auto_exit(bot, chat_id, user_id, state)
    )


# ── Inline keyboards ──────────────────────────────────────────────────────────

def _sphere_inline_kb() -> InlineKeyboardMarkup:
    """Full sphere picker shown when '📝 Записать →' is pressed."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌙 Сон",        callback_data="log_sphere:sleep"),
            InlineKeyboardButton(text="🍽 Питание",    callback_data="log_sphere:nutrition"),
            InlineKeyboardButton(text="💪 Тренировка", callback_data="log_sphere:training"),
        ],
        [
            InlineKeyboardButton(text="🌍 Языки",      callback_data="log_sphere:languages"),
            InlineKeyboardButton(text="💡 Идеи",       callback_data="log_sphere:ideas"),
            InlineKeyboardButton(text="🎸 Гитара",     callback_data="log_sphere:guitar"),
        ],
        [
            InlineKeyboardButton(text="📊 Контекст",   callback_data="log_sphere:context"),
            InlineKeyboardButton(text="🛒 Список",     callback_data="log_sphere:wishlist"),
        ],
    ])


def _lang_kb() -> InlineKeyboardMarkup:
    """Language picker shown when '🌍 Языки' button is pressed."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇩🇪 Немецкий", callback_data="lang:german"),
        InlineKeyboardButton(text="🇷🇴 Румынский", callback_data="lang:romanian"),
    ]])


def _cmds_inline_kb() -> InlineKeyboardMarkup:
    """Technical commands shown when '⚙️ Команды' button is pressed."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Сегодня",   callback_data="cmd_exec:today"),
            InlineKeyboardButton(text="📋 Завтра",    callback_data="cmd_exec:tomorrow"),
        ],
        [
            InlineKeyboardButton(text="📋 Задачи",    callback_data="cmd_exec:pending"),
            InlineKeyboardButton(text="🧹 Очистить",  callback_data="cmd_exec:cleanup"),
        ],
        [
            InlineKeyboardButton(text="🎯 Фокус",     callback_data="cmd_exec:focus"),
            InlineKeyboardButton(text="🔍 Аудит",     callback_data="cmd_exec:audit"),
        ],
        [
            InlineKeyboardButton(text="↩ Отменить",   callback_data="cmd_exec:undo"),
            InlineKeyboardButton(text="💢 Мотивация", callback_data="cmd_exec:motivate"),
        ],
        [
            InlineKeyboardButton(text="📅 День",       callback_data="cmd_exec:dayplan"),
            InlineKeyboardButton(text="🔁 Повтор",     callback_data="cmd_exec:recurring"),
        ],
        [
            InlineKeyboardButton(text="⏹ Стоп",       callback_data="cmd_exec:stop"),
            InlineKeyboardButton(text="❓ Справка",    callback_data="cmd_exec:help"),
        ],
    ])


def _undo_sphere_kb() -> InlineKeyboardMarkup:
    """Sphere picker for undo operation."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌙 Сон",        callback_data="undo_sphere:sleep"),
            InlineKeyboardButton(text="🍽 Питание",    callback_data="undo_sphere:nutrition"),
            InlineKeyboardButton(text="💪 Тренировка", callback_data="undo_sphere:training"),
        ],
        [
            InlineKeyboardButton(text="🇩🇪 Немецкий",  callback_data="undo_sphere:german"),
            InlineKeyboardButton(text="🇷🇴 Румынский", callback_data="undo_sphere:romanian"),
            InlineKeyboardButton(text="💡 Идеи",       callback_data="undo_sphere:ideas"),
        ],
        [
            InlineKeyboardButton(text="📊 Контекст",   callback_data="undo_sphere:context"),
            InlineKeyboardButton(text="🛒 Список",     callback_data="undo_sphere:wishlist"),
        ],
    ])


def _cancel_kb() -> InlineKeyboardMarkup:
    """Shown when entering a mode — before any entry is made."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✗ Отмена", callback_data="cancel_log"),
    ]])


def _entry_kb(filename: str) -> InlineKeyboardMarkup:
    """Shown after each log entry: done / write more / undo last."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Готово", callback_data="done_log"),
            InlineKeyboardButton(text="📝 Ещё", callback_data="more_log"),
        ],
        [
            InlineKeyboardButton(text="↩ Отменить", callback_data=f"undo_log:{filename}"),
        ],
    ])


# ── File auto-create ─────────────────────────────────────────────────────────

async def _ensure_log_file(filename: str) -> None:
    """Create _bot/{filename} with minimal header if it doesn't exist."""
    path = Path(settings.obsidian_vault_path) / "_bot" / filename
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        sphere_name = filename.replace(".md", "").replace("_", " ").capitalize()
        path.write_text(f"# {sphere_name}\n\n## Log\n", encoding="utf-8")
        logger.info("Auto-created log file: %s", path)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sphere_by_button(btn: str) -> dict:
    return _SPHERES[btn]


async def _write_entry(filename: str, text: str) -> tuple[bool, str]:
    """Write formatted entry to file. Returns (ok, message)."""
    await _ensure_log_file(filename)
    entry = _format_entry(filename, text)
    result = await append_to_bot_log(filename, entry)
    ok = not (result.startswith("Ошибка") or result.startswith("Файл"))
    return ok, entry


async def _enter_state(message: Message, state: FSMContext, btn_text: str) -> None:
    cfg = _sphere_by_button(btn_text)
    await state.set_state(cfg["state"])
    await state.update_data(filename=cfg["file"], label=cfg["label"])
    user_id = message.from_user.id if message.from_user else 0
    _start_timeout(message.bot, message.chat.id, user_id, state)
    await message.answer(_STATE_PROMPTS[btn_text], reply_markup=_cancel_kb())


async def _enter_state_from_callback(callback: CallbackQuery, state: FSMContext, btn_text: str) -> None:
    """Same as _enter_state but source is a callback — user_id from callback.from_user."""
    cfg = _sphere_by_button(btn_text)
    await state.set_state(cfg["state"])
    await state.update_data(filename=cfg["file"], label=cfg["label"])
    user_id = callback.from_user.id if callback.from_user else 0
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    _start_timeout(callback.message.bot, callback.message.chat.id, user_id, state)
    await callback.message.answer(_STATE_PROMPTS[btn_text], reply_markup=_cancel_kb())
    await callback.answer()


# ── Handlers ─────────────────────────────────────────────────────────────────

# 1a. "📝 Записать →" — показывает inline picker со всеми сферами
@log_router.message(F.text == _WRITE_BUTTON)
async def handle_write_button(message: Message) -> None:
    await message.answer("Выбери сферу:", reply_markup=_sphere_inline_kb())


# 1a2. "📋 Бэклог" — задачи без даты
@log_router.message(F.text == _BACKLOG_BUTTON)
async def handle_backlog_button(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    from app.storage.db import SessionLocal
    from app.storage.task_repo import TaskRepo
    with SessionLocal() as session:
        tasks = TaskRepo(session).get_backlog_tasks(user_id, limit=15)
    if not tasks:
        await message.answer("📋 Бэклог пуст.")
    else:
        lines = [f"📋 Бэклог ({len(tasks)}):\n"]
        for i, t in enumerate(tasks, 1):
            lines.append(f"{i}. {t.text}")
        await message.answer("\n".join(lines))


# 1b. "🎯 Что делать?" — выходит из любого LogState и вызывает next_step
@log_router.message(F.text == _NEXT_BTN)
async def handle_what_todo_btn(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    _cancel_timeout(user_id)
    await state.clear()
    from app.bot.handlers.message_router import _run_next_step  # lazy import
    await _run_next_step(message, user_id)


# 1c. Sphere picker callback → enter the selected sphere state
@log_router.callback_query(F.data.startswith("log_sphere:"))
async def cb_sphere_select(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 1)[1]
    if key == "languages":
        try:
            await callback.message.edit_reply_markup(reply_markup=_lang_kb())
        except Exception:
            await callback.message.answer("Выбери язык:", reply_markup=_lang_kb())
        await callback.answer()
        return
    btn_text = _KEY_TO_BTN.get(key)
    if not btn_text:
        await callback.answer("Неизвестная сфера.", show_alert=True)
        return
    await _enter_state_from_callback(callback, state, btn_text)


# 1d. "⚙️ Команды" → inline menu с техническими командами
@log_router.message(F.text == _CMDS_BUTTON)
async def handle_cmds_button(message: Message) -> None:
    await message.answer("Команды:", reply_markup=_cmds_inline_kb())


# 1e. Команды — callback dispatch
@log_router.callback_query(F.data.startswith("cmd_exec:"))
async def cb_cmd_exec(callback: CallbackQuery, state: FSMContext) -> None:
    cmd = callback.data.split(":", 1)[1]
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()

    user_id = callback.from_user.id if callback.from_user else 0

    if cmd in ("today", "tomorrow"):
        from app.bot.handlers.message_router import _run_day_view
        await _run_day_view(callback.message, user_id, "сегодня" if cmd == "today" else "завтра")

    elif cmd == "pending":
        from app.bot.handlers.commands import _task_priority_icon, _task_priority_score
        from app.storage.db import SessionLocal
        from app.storage.task_repo import TaskRepo
        with SessionLocal() as session:
            tasks = TaskRepo(session).list_recent(limit=15)
        if not tasks:
            await callback.message.answer("Задач пока нет.")
        else:
            tasks.sort(key=lambda t: (-_task_priority_score(t), -t.id))
            lines = ["📋 Задачи (по приоритету):\n"]
            for i, task in enumerate(tasks, start=1):
                icon = _task_priority_icon(task)
                date_str = task.suggested_date or "бэклог"
                lines.append(f"{icon} {i}. {date_str} — {task.text} [{task.status}]")
            await callback.message.answer("\n".join(lines))

    elif cmd == "cleanup":
        from app.storage.db import SessionLocal
        from app.domain.task_service import TaskService
        from app.storage.dialog_repo import DialogRepo
        with SessionLocal() as session:
            svc = TaskService(session)
            count_tasks = svc.cleanup_stale_pending(older_than_hours=1)
            count_phantoms = svc.cleanup_query_phantoms(user_id)
            count_history = DialogRepo(session).purge_artifacts(user_id)
            session.commit()
        parts: list[str] = []
        if count_tasks + count_phantoms:
            parts.append(f"{count_tasks + count_phantoms} устаревших/phantom задач")
        if count_history:
            parts.append(f"{count_history} артефактов из истории")
        await callback.message.answer(
            f"🧹 Очищено: {', '.join(parts)}." if parts else "✅ Нечего чистить."
        )

    elif cmd == "undo":
        await callback.message.answer("Что отменить?", reply_markup=_undo_sphere_kb())

    elif cmd == "motivate":
        from app.bot.handlers.motivation import _send_motivation
        await _send_motivation(callback.message)

    elif cmd == "dayplan":
        from app.bot.handlers.message_router import _run_day_plan
        await _run_day_plan(callback.message, user_id)

    elif cmd == "recurring":
        from app.storage.db import SessionLocal as _SL
        from app.storage.recurring_repo import RecurringRepo
        with _SL() as _rs:
            items = RecurringRepo(_rs).list_active(user_id)
        if not items:
            await callback.message.answer(
                "Повторяющихся задач нет.\n\nСоздать: «напоминай каждый день пить витамины»"
            )
        else:
            days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
            lines = ["🔁 Повторяющиеся задачи:\n"]
            for rt in items:
                recur = {"daily": "ежедневно", "weekdays": "Пн-Пт"}.get(rt.recurrence)
                if recur is None and rt.recurrence.startswith("weekly:"):
                    try:
                        recur = f"каждый {days_ru[int(rt.recurrence.split(':')[1])]}"
                    except (ValueError, IndexError):
                        recur = rt.recurrence
                end_str = f" до {rt.end_date}" if rt.end_date else ""
                time_str = f" {rt.event_time}" if rt.event_time else ""
                lines.append(f"{rt.id}. {rt.text} — {recur}{time_str}{end_str}")
            await callback.message.answer("\n".join(lines))

    elif cmd == "focus":
        from app.bot.handlers.focus_handler import _is_active, _focus
        from app.storage.db import SessionLocal
        from app.storage.task_repo import TaskRepo
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup as IKM
        if _is_active():
            task_text = _focus.get("task_text", "")
            cycle = _focus.get("cycle", 0)
            await callback.message.answer(
                f"🎯 Уже в фокусе: «{task_text}» (цикл {cycle + 1})\n/stopfocus — завершить"
            )
        else:
            with SessionLocal() as session:
                tasks = TaskRepo(session).get_today_plan(user_id)
            if not tasks:
                await callback.message.answer(
                    "📝 На сегодня задач нет.\nНапиши: <code>/focus название</code>"
                )
            else:
                rows = [
                    [InlineKeyboardButton(
                        text=t.text[:45],
                        callback_data=f"focus_select:{t.id}",
                    )]
                    for t in tasks[:5]
                ]
                rows.append([InlineKeyboardButton(text="✏️ Другое", callback_data="focus_select:custom")])
                await callback.message.answer(
                    "🎯 Над чем работаешь?",
                    reply_markup=IKM(inline_keyboard=rows),
                )

    elif cmd == "audit":
        from app.storage.db import SessionLocal, ToolAuditLog
        with SessionLocal() as session:
            rows = (
                session.query(ToolAuditLog)
                .filter(ToolAuditLog.user_id == user_id)
                .order_by(ToolAuditLog.id.desc())
                .limit(10)
                .all()
            )
        if not rows:
            await callback.message.answer("Аудит пуст.")
        else:
            lines = ["🔍 Последние tool calls:\n"]
            for r in reversed(rows):
                icon = "✅" if r.ok else "❌"
                ts = r.created_at[11:16]
                args_short = (r.args_json or "")[:60]
                lines.append(f"{icon} {ts} {r.tool_name}({args_short})")
                if not r.ok and r.error_text:
                    lines.append(f"   ↳ {r.error_text[:80]}")
            await callback.message.answer("\n".join(lines))

    elif cmd == "stop":
        _cancel_timeout(user_id)
        await state.clear()
        await callback.message.answer("Режим выключен.")

    elif cmd == "help":
        from app.bot.handlers.commands import _HELP_TEXT
        await callback.message.answer(_HELP_TEXT)


# 1f. Undo sphere callback → удалить последнюю запись в выбранной сфере
@log_router.callback_query(F.data.startswith("undo_sphere:"))
async def cb_undo_sphere(callback: CallbackQuery) -> None:
    sphere_key = callback.data.split(":", 1)[1]
    filename = _SPHERE_ARG_TO_FILE.get(sphere_key)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not filename:
        await callback.answer("Неизвестная сфера.", show_alert=True)
        return
    ok, info = await delete_last_log_entry(filename)
    if ok:
        display = info.lstrip("- ").strip()
        await callback.message.answer(f"↩ Отменено: {display}")
    else:
        await callback.message.answer(f"⚠️ {info}")
    await callback.answer()


# 1g. Language button (text) → show language picker (backward compat / manual input)
@log_router.message(F.text == _LANG_BUTTON)
async def handle_lang_button(message: Message) -> None:
    await message.answer("Выбери язык:", reply_markup=_lang_kb())


# 1b. Language picker callback → enter the selected language state
@log_router.callback_query(F.data.startswith("lang:"))
async def cb_lang_select(callback: CallbackQuery, state: FSMContext) -> None:
    lang = callback.data.split(":", 1)[1]
    btn_text = "📖 Немецкий" if lang == "german" else "🇷🇴 Румынский"
    cfg = _sphere_by_button(btn_text)
    await state.set_state(cfg["state"])
    await state.update_data(filename=cfg["file"], label=cfg["label"])
    user_id = callback.from_user.id if callback.from_user else 0
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    _start_timeout(callback.message.bot, callback.message.chat.id, user_id, state)
    await callback.message.answer(_STATE_PROMPTS[btn_text], reply_markup=_cancel_kb())
    await callback.answer()


# 1c. Sphere button — works in any state (including None and other LogStates)
@log_router.message(F.text.in_(_BUTTON_TEXTS))
async def handle_log_button(message: Message, state: FSMContext) -> None:
    await _enter_state(message, state, message.text)


# 2. Any text while in LogState (commands handled by their own handlers below)
@log_router.message(StateFilter(LogState), F.text, ~F.text.startswith("/"))
async def handle_log_entry(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    user_id = message.from_user.id if message.from_user else 0

    # Stop words → exit state
    if _STOP_RE.match(text):
        _cancel_timeout(user_id)
        await state.clear()
        await message.answer("Режим логирования выключен.")
        return

    # Escape: сообщение явно не является записью → выходим из режима и в LLM
    if _LOG_ESCAPE_RE.search(text):
        data = await state.get_data()
        label = data.get("label", "")
        _cancel_timeout(user_id)
        await state.clear()
        note = f"(вышел из режима «{label}») " if label else ""
        logger.info("Log escape: %s→ LLM for %r", note, text)
        from app.bot.handlers.message_router import _run_llm_chat  # lazy import
        await _run_llm_chat(message, user_id, text)
        return

    data = await state.get_data()
    filename: str = data.get("filename", "ivan_context.md")
    label: str = data.get("label", "Контекст")

    ok, entry = await _write_entry(filename, text)

    if ok:
        await state.update_data(last_entry=entry)
        _start_timeout(message.bot, message.chat.id, user_id, state)  # reset timer
        await message.answer(f"✅ {label}: {entry}", reply_markup=_entry_kb(filename))
    else:
        await message.answer(f"⚠️ Не смог записать: {entry}")


# ── Inline button callbacks ───────────────────────────────────────────────────

@log_router.callback_query(F.data == "done_log")
async def cb_done_log(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id if callback.from_user else 0
    _cancel_timeout(user_id)
    data = await state.get_data()
    label = data.get("label", "")
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(f"✅ Записано в «{label}»." if label else "Готово.")
    await callback.answer()


@log_router.callback_query(F.data == "more_log")
async def cb_more_log(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id if callback.from_user else 0
    _start_timeout(callback.message.bot, callback.message.chat.id, user_id, state)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Пиши следующую запись.")


@log_router.callback_query(F.data == "cancel_log")
async def cb_cancel_log(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = callback.from_user.id if callback.from_user else 0
    _cancel_timeout(user_id)
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Отменено.")


# ── Undo callback (inline button "↩ Отменить") ───────────────────────────────

@log_router.callback_query(F.data.startswith("undo_log:"))
async def cb_undo_log(callback: CallbackQuery) -> None:
    filename = callback.data.split(":", 1)[1]
    ok, info = await delete_last_log_entry(filename)

    if ok:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        display = info.lstrip("- ").strip()
        await callback.message.answer(f"↩ Отменено: {display}")
    else:
        await callback.answer(info, show_alert=True)

    await callback.answer()


# ── Slash commands ────────────────────────────────────────────────────────────

async def _cmd_log(
    message: Message,
    state: FSMContext,
    command: CommandObject,
    btn_text: str,
) -> None:
    """Shared handler for all logging slash commands."""
    args = (command.args or "").strip()
    cfg = _SPHERES[btn_text]
    user_id = message.from_user.id if message.from_user else 0

    if args:
        await state.set_state(cfg["state"])
        await state.update_data(filename=cfg["file"], label=cfg["label"])
        ok, entry = await _write_entry(cfg["file"], args)
        if ok:
            await state.update_data(last_entry=entry)
            _start_timeout(message.bot, message.chat.id, user_id, state)
            await message.answer(f"✅ {cfg['label']}: {entry}", reply_markup=_entry_kb(cfg["file"]))
        else:
            await state.clear()
            await message.answer(f"⚠️ {entry}")
    else:
        await _enter_state(message, state, btn_text)


@log_router.message(Command("sleep"))
async def cmd_sleep(message: Message, state: FSMContext, command: CommandObject) -> None:
    await _cmd_log(message, state, command, "🌙 Сон")


@log_router.message(Command("meal"))
async def cmd_meal(message: Message, state: FSMContext, command: CommandObject) -> None:
    await _cmd_log(message, state, command, "🍽 Питание")


@log_router.message(Command("workout"))
async def cmd_workout(message: Message, state: FSMContext, command: CommandObject) -> None:
    await _cmd_log(message, state, command, "💪 Тренировка")


@log_router.message(Command("german", "de"))
async def cmd_german(message: Message, state: FSMContext, command: CommandObject) -> None:
    await _cmd_log(message, state, command, "📖 Немецкий")


@log_router.message(Command("romanian", "ro"))
async def cmd_romanian(message: Message, state: FSMContext, command: CommandObject) -> None:
    await _cmd_log(message, state, command, "🇷🇴 Румынский")


@log_router.message(Command("ideas", "idea"))
async def cmd_ideas(message: Message, state: FSMContext, command: CommandObject) -> None:
    await _cmd_log(message, state, command, "💡 Идеи")


@log_router.message(Command("ctx", "context"))
async def cmd_ctx(message: Message, state: FSMContext, command: CommandObject) -> None:
    await _cmd_log(message, state, command, "📊 Контекст")


@log_router.message(Command("wish", "wishlist"))
async def cmd_wish(message: Message, state: FSMContext, command: CommandObject) -> None:
    await _cmd_log(message, state, command, "🛒 Список")


@log_router.message(Command("guitar"))
async def cmd_guitar(message: Message, state: FSMContext, command: CommandObject) -> None:
    await _cmd_log(message, state, command, "🎸 Гитара")


@log_router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id if message.from_user else 0
    current = await state.get_state()
    if current is None:
        await message.answer("Нет активного режима.")
        return
    _cancel_timeout(user_id)
    await state.clear()
    await message.answer("Режим выключен.")


# ── /undo [сфера] ─────────────────────────────────────────────────────────────

@log_router.message(Command("undo"))
async def cmd_undo(message: Message, state: FSMContext, command: CommandObject) -> None:
    """Отменить последнюю запись в активной сфере или в указанной: /undo sleep"""
    arg = (command.args or "").strip().lower()

    if arg:
        filename = _SPHERE_ARG_TO_FILE.get(arg)
        if not filename:
            spheres = ", ".join(sorted({v.replace(".md", "") for v in _SPHERE_ARG_TO_FILE.values()}))
            await message.answer(
                f"Неизвестная сфера «{arg}».\nДоступные: {spheres}"
            )
            return
    else:
        data = await state.get_data()
        filename = data.get("filename")
        if not filename:
            await message.answer(
                "Нет активного режима. Укажи сферу явно:\n"
                "/undo sleep — сон\n/undo meal — питание\n/undo workout — тренировка"
            )
            return

    ok, info = await delete_last_log_entry(filename)
    if ok:
        display = info.lstrip("- ").strip()
        await message.answer(f"↩ Отменено: {display}")
    else:
        await message.answer(f"⚠️ {info}")


# ── Sleep time parser ─────────────────────────────────────────────────────────

_RU_NUM: dict[str, int] = {
    "ноль": 0, "нуля": 0,
    "полночь": 0, "полуночи": 0, "полночью": 0, "полуночью": 0,
    "час": 1, "один": 1, "одного": 1, "одна": 1, "одной": 1,
    "два": 2, "двух": 2, "двум": 2, "двое": 2,
    "три": 3, "трёх": 3, "трех": 3, "трём": 3,
    "четыре": 4, "четырёх": 4, "четырех": 4,
    "пять": 5, "пяти": 5,
    "шесть": 6, "шести": 6,
    "семь": 7, "семи": 7,
    "восемь": 8, "восьми": 8,
    "девять": 9, "девяти": 9,
    "десять": 10, "десяти": 10,
    "одиннадцать": 11, "одиннадцати": 11,
    "двенадцать": 12, "двенадцати": 12,
    "полдень": 12, "полудня": 12,
}

# Time token: "HH:MM", "HH", or Russian word
def _parse_token(tok: str) -> tuple[int, int] | None:
    """Return (hour, minute) from "HH:MM", "HH MM", "H", or Russian word. None if unparseable."""
    tok = tok.strip().lower()
    if tok in _RU_NUM:
        return (_RU_NUM[tok], 0)
    # "HH:MM" or "HH MM" (space-separated minutes)
    m = re.match(r"^(\d{1,2})[:\s](\d{2})$", tok)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    # bare hour "HH"
    m = re.match(r"^(\d{1,2})$", tok)
    if m:
        return (int(m.group(1)), 0)
    return None


def _normalize_start(sh: int, sm: int, eh: int, em: int) -> int:
    """
    Interpret ambiguous start hour for sleep:
      12           → 0   (midnight)
      10, 11       → +12 (clearly PM: 22, 23)
      6–9          → +12 if raw duration would be > 16h (e.g. "9–7" → 21–7)
      0–5, 13–23   → as-is
    """
    if sh == 12:
        return 0
    if sh in (10, 11):
        return sh + 12
    if 6 <= sh <= 9:
        raw = (eh * 60 + em) - (sh * 60)
        if raw <= 0:
            raw += 1440
        if raw > 16 * 60:
            return sh + 12
    return sh


def _duration_str(sh: int, sm: int, eh: int, em: int) -> str:
    s = sh * 60 + sm
    e = eh * 60 + em
    if e <= s:
        e += 1440
    total = e - s
    h, m = divmod(total, 60)
    return f"{h}ч {m}м" if m else f"{h}ч"


def _fmt(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


# ── Time range patterns (tried in order in parse_sleep_time) ─────────────────

# "HH MM - HH MM" — space-separated hours/minutes; must be tried BEFORE colon pattern
_RANGE_SPACE_RE = re.compile(
    r"(\d{1,2})\s+(\d{2})\s*[-–—]\s*(\d{1,2})(?:\s+(\d{2}))?",
)

# "HH:MM-HH:MM" or "HH-HH" — colon or bare hours
_RANGE_COLON_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?",
)

# "с HH:MM до HH:MM" / "с девяти до шести" / "с 00 30 до 10 10"
_FROM_TO_RE = re.compile(
    r"(?:с|со)\s+(\d{1,2}(?:[:\s]\d{2})?|[\w]+)\s+до\s+(\d{1,2}(?:[:\s]\d{2})?|[\w]+)",
    re.IGNORECASE,
)

# "лег в 23" / "лег в 23:30" / "лег в 23 30" / "лег в полночь"
_BED_RE = re.compile(
    r"лег[а]?\s+(?:в\s+)?(\d{1,2}(?:[:\s]\d{2})?|[\w]+)",
    re.IGNORECASE,
)

# "встал в 7" / "проснулся в 7:30" / "проснулся в 7 30"
_WAKE_RE = re.compile(
    r"(?:встал[а]?|проснул[ась]*)\s+(?:в\s+)?(\d{1,2}(?:[:\s]\d{2})?|[\w]+)",
    re.IGNORECASE,
)

# All patterns that carry time info — used for stripping
_ALL_TIME_RES = [_FROM_TO_RE, _BED_RE, _WAKE_RE, _RANGE_SPACE_RE, _RANGE_COLON_RE]


def _try_parse_time(sh: int, sm: int, eh: int, em: int) -> str | None:
    """Validate and format parsed sleep interval. Returns None if hours/minutes invalid."""
    if sm > 59 or em > 59:
        return None
    sh = _normalize_start(sh, sm, eh, em)
    if sh > 23:
        return None
    return f"{_fmt(sh, sm)}–{_fmt(eh, em)} ({_duration_str(sh, sm, eh, em)})"


def parse_sleep_time(text: str) -> str | None:
    """
    Extract sleep interval from free text.
    Returns "HH:MM–HH:MM (Xч Yм)" or None.
    """
    # 1. "с X до Y"
    m = _FROM_TO_RE.search(text)
    if m:
        s = _parse_token(m.group(1))
        e = _parse_token(m.group(2))
        if s and e:
            result = _try_parse_time(s[0], s[1], e[0], e[1])
            if result:
                return result

    # 2. "лег в X ... встал в Y"
    bed = _BED_RE.search(text)
    wake = _WAKE_RE.search(text)
    if bed and wake:
        s = _parse_token(bed.group(1))
        e = _parse_token(wake.group(1))
        if s and e:
            result = _try_parse_time(s[0], s[1], e[0], e[1])
            if result:
                return result

    # 3. Space-separated "HH MM - HH MM" — must be before colon pattern
    m = _RANGE_SPACE_RE.search(text)
    if m:
        sh, sm_, eh, em_ = (
            int(m.group(1)), int(m.group(2)),
            int(m.group(3)), int(m.group(4) or 0),
        )
        result = _try_parse_time(sh, sm_, eh, em_)
        if result:
            return result

    # 4. Colon-separated "HH:MM-HH:MM" or bare "HH-HH"
    m = _RANGE_COLON_RE.search(text)
    if m:
        sh, sm_, eh, em_ = (
            int(m.group(1)), int(m.group(2) or 0),
            int(m.group(3)), int(m.group(4) or 0),
        )
        result = _try_parse_time(sh, sm_, eh, em_)
        if result:
            return result

    return None


def _strip_time_from_text(text: str) -> str:
    """Remove all recognized time patterns, return leftover comment."""
    result = text
    for pattern in _ALL_TIME_RES:
        result = pattern.sub("", result)
    result = re.sub(r"^\s*[,;.\-—]\s*", "", result)
    result = re.sub(r"\s*[,;.]\s*$", "", result)
    return result.strip()


# ── Entry formatter ──────────────────────────────────────────────────────────

def _format_entry(filename: str, text: str) -> str:
    """Format log entry. For sleep: extract and format time range."""
    if filename == "sleep.md":
        sleep_time = parse_sleep_time(text)
        if sleep_time:
            rest = _strip_time_from_text(text)
            return sleep_time + (f" — {rest}" if rest else "")
    return text
