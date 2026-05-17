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
            KeyboardButton(text="🌙 Сон"),
            KeyboardButton(text="🍽 Питание"),
            KeyboardButton(text="💪 Тренировка"),
        ],
        [
            KeyboardButton(text="🌍 Языки"),
            KeyboardButton(text="💡 Идеи"),
            KeyboardButton(text="📊 Контекст"),
        ],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

_LANG_BUTTON = "🌍 Языки"

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

def _lang_kb() -> InlineKeyboardMarkup:
    """Language picker shown when '🌍 Языки' button is pressed."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇩🇪 Немецкий", callback_data="lang:german"),
        InlineKeyboardButton(text="🇷🇴 Румынский", callback_data="lang:romanian"),
    ]])


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


# ── Handlers ─────────────────────────────────────────────────────────────────

# 1a. Language button → show language picker inline keyboard
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
    await callback.message.answer(f"✅ {label} записан." if label else "Готово.")
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
