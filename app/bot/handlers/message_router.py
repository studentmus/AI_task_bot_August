import asyncio
import json
import logging
import re
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.types import Message

from app.bot.handlers.commands import commands_router
from app.bot.handlers.tasks import _build_card, _build_keyboard, tasks_router
from app.domain.task_service import TaskService
from app.llm.deepseek_client import call_deepseek_chat
from app.llm.obsidian_tools import append_obsidian_log, _resolve_sphere
from app.llm.prompt_builder import build_messages
from app.llm.tool_executor import execute_tool_call
from app.llm.tool_registry import TOOLS
from app.parsing.task_engine import ParseResult, parse_task
from app.storage.db import SessionLocal
from app.storage.task_repo import TaskRepo


logger = logging.getLogger(__name__)

# Теги, которые DeepSeek иногда «протекает» в финальный content.
# Fullwidth vertical line U+FF5C (｜) — характерный маркер DeepSeek.
_DEEPSEEK_TAG_RE = re.compile(r"</?｜[^>]*>", re.IGNORECASE)
# DeepSeek иногда возвращает Markdown-разметку, хотя бот использует HTML-режим.
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"\*(.+?)\*", re.DOTALL)
_MD_CODE_RE = re.compile(r"`(.+?)`", re.DOTALL)


def _clean_reply(text: str) -> str:
    text = _DEEPSEEK_TAG_RE.sub("", text)
    text = _MD_BOLD_RE.sub(r"\1", text)    # **bold** → bold
    text = _MD_ITALIC_RE.sub(r"\1", text)  # *italic* → italic
    text = _MD_CODE_RE.sub(r"\1", text)    # `code` → code
    return text.strip()


main_router = Router(name="main")

# Порядок важен: commands → callbacks/FSM → text dispatch (этот модуль)
main_router.include_router(commands_router)
main_router.include_router(tasks_router)


# ---------------------------------------------------------------------------
# Текстовый диспетчер: rule-based → LLM tool-calling
# ---------------------------------------------------------------------------

# Фразы, адресованные ассистенту как команды (не записи фактов).
# При совпадении пропускаем rule-based парсер и идём сразу в LLM.
COMMAND_KEYWORDS = [
    "что у меня", "покажи", "перенес", "перенеси", "выполнил",
    "удали", "удалить", "отмени", "добавь задачу", "поставь задачу",
    "напомни", "отложи", "какие задачи", "план на", "запомни",
    # Глаголы изменения без местоимения (иначе попадают в _CONTEXT_EDIT_RE)
    "поставь", "измени", "поменяй", "переименуй",
]

# ---------------------------------------------------------------------------
# Conversation Guard: отказы и вежливые ответы → сразу LLM, минуя все парсеры
# ---------------------------------------------------------------------------

# Мягкая проверка: сообщение НАЧИНАЕТСЯ с разговорного слова ИЛИ содержит фразу-отказ.
# Не требует, чтобы 100% слов были «разговорными» — достаточно любого из маркеров.
_CONVERSATION_RE = re.compile(
    # Начинается с разговорного слова/фразы
    r"^(?:нет|да|ок|окей|хорошо|спасибо|понял|понятно|ладно|давай|ага|угу"
    r"|отлично|супер|класс|пока|стоп|всё)\b"
    # ИЛИ содержит явный отказ/отбой в любом месте
    r"|\bне\s+нужно\b"
    r"|\bне\s+надо\b"
    r"|\bотбой\b"
    r"|\bне\s+создавай\b"
    r"|\bне\s+добавляй\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Контекстное редактирование: местоимения + глаголы изменения → LLM (move_task)
# ---------------------------------------------------------------------------

# Перехватывает фразы типа "поставь ей время", "измени её дату", "сделай это на 15:00".
# Любое совпадение = почти наверняка контекстная правка существующей задачи.
_CONTEXT_EDIT_RE = re.compile(
    # Только местоимения-ссылки на конкретную задачу из контекста диалога.
    # Глаголы убраны: "поставь/измени/поменяй" без местоимения — это новые команды,
    # они идут через COMMAND_KEYWORDS. Оставляем только местоимения.
    r"\b(?:ей|её|ему|им|эту|этот|этой|эта|этого|этой)\b",
    re.IGNORECASE,
)


# Гарда чтения: запросы на просмотр/чтение данных. Rule-based парсер не должен
# их видеть — иначе создаёт фантомные задачи вроде "показать, что записалось в sleep".
_READ_REQUEST_RE = re.compile(
    r"\b(?:прочитай|прочти|посмотри|выведи|загляни)\b"
    r"|\bчто\s+(?:записалось|записано|там|в\s+логе|в\s+дневнике|получилось|попало)\b"
    r"|\bкакие\s+(?:записи|данные|показатели|итоги)\b"
    r"|\bкуда\s+(?:записалось|записало|попало|пишет(?:ся)?)\b"
    r"|\bпоказать\s+(?:лог|запис|данные|что)\b",
    re.IGNORECASE,
)

# Гарда «не спится»: логирует факт + даёт лёгкую рекомендацию.
# Проверяется ДО _NEXT_STEP_RE чтобы не уйти в обычный next-step с late-night gate.
_CANT_SLEEP_RE = re.compile(
    r"\bне\s+спится\b"
    r"|\bне\s+могу\s+спать\b"
    r"|\bбессонница\b"
    r"|\bне\s+сплю\b"
    r"|\bпросыпаюсь\b"
    r"|\bпроснулся\s+и\s+не\s+сплю\b",
    re.IGNORECASE,
)

# Гарда «следующего шага»: детектирует запросы типа "что делать", "следующий шаг".
# Проверяется ДО _QUESTION_RE, чтобы получить специализированный ответ, а не общий чат.
_NEXT_STEP_RE = re.compile(
    r"\bчто\s+(?:мне\s+)?(?:сейчас\s+)?делать\b"
    r"|\bследующий\s+шаг\b"
    r"|\bчто\s+делаем\b"
    r"|\bпосоветуй\s+(?:что|куда|как)\b"
    r"|\bс\s+чего\s+(?:начать|начнём|начнем)\b"
    r"|\bчто\s+в\s+приоритете\b"
    r"|\bчто\s+важнее\b"
    r"|\bкуда\s+двигаться\b"
    r"|\bдай\s+(?:один\s+)?шаг\b",
    re.IGNORECASE,
)

# Гарда для вопросов: rule-based парсер не должен трогать вопросительные сообщения.
# Знак «?» надёжно перехватывает большинство случаев; остальные паттерны — русские
# вопросительные конструкции без знака вопроса.
_QUESTION_RE = re.compile(
    r"\?"                           # любой знак вопроса
    r"|есть ли\b"                   # "есть ли у меня задача"
    r"|есть задача\b"               # "завтра есть задача попить воды"
    r"|есть у меня\b"               # "есть у меня встреча"
    r"|^есть\b"                     # начинается с "есть ..."
    r"|^не напомнил\b"              # "не напомнил про X"
    r"|будет ли\b"
    r"|\bне\s+вижу\b"               # "не вижу в календаре"
    r"|\bне\s+нашёл\b|\bне\s+нашел\b"  # "не нашёл задачу"
    r"|\bне\s+показывает\b"         # "не показывает событие"
    r"|\bпочему\b|\bзачем\b|\bкогда\b"  # вопросительные слова без "?"
    r"|\bкуда\s+делась\b|\bгде\s+(?:задача|событие|запись)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Obsidian-лог: паттерны распознавания
# ---------------------------------------------------------------------------

# ── Паттерн A: «Запиши в питание: текст» / «В питание запиши: текст» ──────
# Захватывает ОБА элемента: сферу и текст записи → прямой вызов без LLM.
_OBSIDIAN_LOG_FULL_RE = re.compile(
    r"^(?:"
    # verb-first: "запиши в питание: текст" или "запиши в питание текст"
    r"(?:запиши|запишите|добавь|добавьте|отметь|отметьте|зафиксируй|зафиксируйте|залогируй|залогируйте)"
    r"\s+в\s+(?P<sphere1>[^\s:,]+)[:\s]+(?P<entry1>\S.*)"
    r"|"
    # sphere-first: "в питание запиши: текст"
    r"в\s+(?P<sphere2>[^\s:,]+)\s+(?:запиши|запишите|добавь|добавьте|отметь|отметьте|зафиксируй|зафиксируйте|залогируй|залогируйте)[:\s]+(?P<entry2>\S.*)"
    r")",
    re.IGNORECASE,
)

# ── Паттерн B: «Текст, запиши в питание» (сначала факт, потом директива) ──
_ENTRY_FIRST_LOG_RE = re.compile(
    r"^(?P<entry3>\S.+?)[,;]\s*(?:запиши|добавь|отметь|зафиксируй|залогируй)\s+в\s+(?P<sphere3>[^\s:,.]+)\s*$",
    re.IGNORECASE,
)

# ── Паттерн C: сфера+глагол без текста (нет what именно записать → LLM спросит) ─
_OBSIDIAN_LOG_RE = re.compile(
    r"^(?:"
    r"(?:запиши|запишите|добавь|добавьте|отметь|отметьте|зафиксируй|зафиксируйте|залогируй|залогируйте)"
    r"\s+в\s+(?P<sphere1>[^\s:]+)"
    r"|"
    r"в\s+(?P<sphere2>[^\s:]+)\s+(?:запиши|запишите|добавь|добавьте|отметь|отметьте|зафиксируй|зафиксируйте|залогируй|залогируйте)"
    r")",
    re.IGNORECASE,
)

# ── Паттерн D: прошедшее время → LLM решает: complete_task или append_obsidian_log ─
# Две группы глаголов:
#   а) трекинг-факты (еда, спорт, сон) — скорее всего лог
#   б) завершение задачи (купил, позвонил, сходил) — скорее всего complete_task
# LLM сам выбирает инструмент, сверяясь со списком активных задач из промпта.
_FACT_VERBS_RE = re.compile(
    r"\b(?:"
    # Трекинг: еда, спорт, сон, физиология
    r"съел|съела|съели"
    r"|выпил|выпила|выпили"
    r"|пожал|пожала|пожали"
    r"|потренировался|потренировалась|потренировались"
    r"|пробежал|пробежала|пробежали"
    r"|поднял|подняла|подняли"
    r"|замерил|замерила|замерили"
    r"|лёг|лег|легла|легли"
    r"|встал|встала|встали"
    r"|проснулся|проснулась|проснулись"
    r"|спал|спала|спали"
    r"|жал|жала|жали"
    r"|присел|приседал|приседала"
    r"|подтянулся|подтянулась|подтянулись"
    r"|покушал|покушала|поел|поела"
    r"|поужинал|поужинала|позавтракал|позавтракала|пообедал|пообедала"
    r"|завтракал|завтракала|завтракали|обедал|обедала|обедали|ужинал|ужинала|ужинали"
    r"|перекусил|перекусила|перекусили"
    r"|взвесился|взвесилась"
    # Завершение задачи: бытовые и рабочие действия
    r"|купил|купила|купили"
    r"|позвонил|позвонила|позвонили"
    r"|сходил|сходила|сходили"
    r"|отправил|отправила|отправили"
    r"|написал|написала|написали"
    r"|закончил|закончила|закончили"
    r"|выполнил|выполнила|выполнили"
    r"|сделал|сделала|сделали"
    r"|оплатил|оплатила|оплатили"
    r"|забронировал|забронировала"
    r"|записался|записалась"
    r"|встретился|встретилась"
    r")\b",
    re.IGNORECASE,
)

# Существительные приёмов пищи в начале сообщения — высокая вероятность лог-записи.
# "Завтрак: каша 200г" → nutrition log; "Завтрак с Ваней в пятницу" → LLM создаст задачу.
# В обоих случаях LLM справится корректно; rule-based парсер не должен трогать эти фразы.
_MEAL_NOUN_RE = re.compile(
    r"^(?:завтрак|обед|ужин|перекус)\b",
    re.IGNORECASE,
)

# Известные сферы — запасной уровень проверки для нестандартного порядка слов.
_LOG_SPHERE_KEYWORDS = [
    "в питание", "в лог", "в журнал", "в дневник", "в протокол",
    "в сон", "в тренировки", "в тренировку", "в спорт",
    "в здоровье", "в финансы", "в работу", "в привычки",
    "в рацион", "в настроение",
]

_LOG_LEADING_VERBS = {"запиши", "добавь", "отметь", "зафиксируй", "залогируй"}

# Слова-продолжения без явной сферы: "еще съел 300г салата", "плюс выпил кофе"
_LOG_CONTINUATION_RE = re.compile(
    r"^(?:еще|ещё|также|плюс|добавь)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Сессионный кэш контекста лога (in-memory, живёт вместе с процессом)
# ---------------------------------------------------------------------------

_LOG_CONTEXT_TTL = 900  # секунд (15 минут)

# user_id → {"sphere": str, "timestamp": datetime}
USER_LOG_CONTEXT: dict[int, dict] = {}


def _set_log_context(user_id: int, sphere: str) -> None:
    USER_LOG_CONTEXT[user_id] = {"sphere": sphere, "timestamp": datetime.now()}


# История диалога хранится в SQLite (app/storage/dialog_repo.py).
# In-memory кэш не нужен — репо читает/пишет напрямую в БД.


def _check_log_continuation(user_id: int, lower: str) -> str | None:
    """Если сообщение — продолжение лога и кэш свежий, возвращает сферу. Иначе None."""
    if not _LOG_CONTINUATION_RE.match(lower):
        return None
    ctx = USER_LOG_CONTEXT.get(user_id)
    if ctx is None:
        return None
    age = (datetime.now() - ctx["timestamp"]).total_seconds()
    if age > _LOG_CONTEXT_TTL:
        return None
    return ctx["sphere"]


def _parse_direct_log_intent(raw: str) -> tuple[str, str] | None:
    """
    Пытается извлечь (sphere, entry) из сообщения без обращения к LLM.
    Покрывает три синтаксиса:
      A  "Запиши в питание: текст"   / "В питание запиши: текст"
      B  "Текст лога, запиши в питание"
    Возвращает (очищенная_сфера, текст_записи) или None.
    """
    m = _OBSIDIAN_LOG_FULL_RE.match(raw)
    if m:
        sphere_raw = (m.group("sphere1") or m.group("sphere2") or "").strip()
        entry = (m.group("entry1") or m.group("entry2") or "").strip()
        sphere = re.sub(r"[^\w]", "", sphere_raw, flags=re.UNICODE).lower()
        if sphere and entry:
            return sphere, entry

    m2 = _ENTRY_FIRST_LOG_RE.match(raw)
    if m2:
        sphere_raw = m2.group("sphere3").strip()
        entry = m2.group("entry3").strip()
        sphere = re.sub(r"[^\w]", "", sphere_raw, flags=re.UNICODE).lower()
        if sphere and entry:
            return sphere, entry

    return None


def _extract_log_sphere(m: re.Match) -> str | None:
    """Возвращает очищенное имя сферы из матча _OBSIDIAN_LOG_RE."""
    raw = m.group("sphere1") or m.group("sphere2") or ""
    cleaned = re.sub(r"[^\w]", "", raw, flags=re.UNICODE).lower()
    return cleaned or None


def _is_obsidian_log_intent(lower: str) -> bool:
    """True если сообщение явно адресовано в сферу, но текст записи не указан."""
    if _OBSIDIAN_LOG_RE.match(lower):
        return True
    first_word = lower.split()[0] if lower.split() else ""
    has_sphere = any(kw in lower for kw in _LOG_SPHERE_KEYWORDS)
    has_verb = any(v in lower for v in _LOG_LEADING_VERBS)
    if first_word == "в":
        return has_sphere and has_verb
    return first_word in _LOG_LEADING_VERBS and has_sphere


# ---------------------------------------------------------------------------
# Диспетчер входящих текстовых сообщений
# Порядок проверок критичен — не менять без понимания всей цепочки.
# ---------------------------------------------------------------------------

@main_router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def dispatch_text(message: Message) -> None:
    raw = message.text.strip()
    user_id = message.from_user.id if message.from_user else 0
    lower = raw.lower()

    # ── 0. НЕ СПИТСЯ — лог + лёгкая рекомендация (раньше next-step чтобы обойти late-night gate)
    if _CANT_SLEEP_RE.search(lower):
        logger.info("Can't sleep detected → log + light activity for %r", raw)
        await _run_cant_sleep(message, user_id)
        return

    # ── 0.5. СЛЕДУЮЩИЙ ШАГ — специализированный engine (энергия + GCal + алёрты) ──
    if _NEXT_STEP_RE.search(lower):
        logger.info("Next-step request → engine for %r", raw)
        await _run_next_step(message, user_id)
        return

    # ── 0.5. ГАРДА ВОПРОСОВ — до любых regex-парсеров ────────────────────────
    # Rule-based парсер умеет только создавать задачи; вопросы он испортит.
    if _QUESTION_RE.search(lower):
        logger.info("Question detected → LLM for %r", raw)
        await _run_llm_chat(message, user_id, raw)
        return

    # ── 0.5. CONVERSATION GUARD: отказы и вежливые ответы → LLM как чат ─────────
    # Мягкая гарда: достаточно начала с разговорного слова или фразы-отказа.
    # "Нет, спасибо, пока не нужно" → LLM; "поставь ей время" пройдёт дальше.
    if _CONVERSATION_RE.search(lower):
        logger.info("Conversation/refusal detected → LLM for %r", raw)
        await _run_llm_chat(message, user_id, raw)
        return

    # ── 1. ПРЯМОЙ ЛОГ: сфера + текст извлекаются regex, LLM не нужен ──────────
    # "Запиши в питание: съел стейк" / "Съел стейк, запиши в питание"
    direct = _parse_direct_log_intent(raw)
    if direct is not None:
        sphere, entry = direct
        logger.info("Direct log (sphere=%s) → append for %r", sphere, raw)
        await _run_log_direct(message, user_id, sphere, entry)
        return

    # ── 2. ЯВНАЯ СФЕРА БЕЗ ТЕКСТА: "Запиши в питание" → LLM уточнит ────────────
    if _is_obsidian_log_intent(lower):
        m = _OBSIDIAN_LOG_RE.match(lower)
        sphere = _extract_log_sphere(m) if m else None
        logger.info("Obsidian log intent (sphere=%s) → LLM for %r", sphere, raw)
        await _run_llm_chat(message, user_id, raw)
        return

    # ── 3. ПРОДОЛЖЕНИЕ ЛОГА: "ещё съел 300г" + свежий кэш ──────────────────────
    continuation_sphere = _check_log_continuation(user_id, lower)
    if continuation_sphere is not None:
        entry = _LOG_CONTINUATION_RE.sub("", raw, count=1).lstrip(":, ").strip() or raw
        logger.info("Log continuation (sphere=%s) → direct append for %r", continuation_sphere, raw)
        await _run_log_direct(message, user_id, continuation_sphere, entry)
        return

    # ── 4. ПРОШЕДШЕЕ ВРЕМЯ → LLM выбирает между complete_task и append_obsidian_log ──
    # LLM получает полный список инструментов + список активных задач в промпте.
    # Если прошедшее действие совпадает с активной задачей → complete_task.
    # Если нет совпадения с задачей, но это трекинг-факт → append_obsidian_log.
    # Проверяется ДО task_engine, чтобы эти сообщения не попали в rule-based парсер.
    if _FACT_VERBS_RE.search(lower):
        logger.info("Past-tense verb detected → LLM (complete_task or log) for %r", raw)
        await _run_llm_chat(message, user_id, raw)
        return

    # ── 4.5. СУЩЕСТВИТЕЛЬНЫЕ ПРИЁМА ПИЩИ в начале → LLM ────────────────────────
    # "Завтрак: каша" → LLM → append_obsidian_log; "Завтрак с Ваней в пятницу" → create_task.
    # Rule-based парсер не умеет различать — LLM справляется с обоими случаями.
    if _MEAL_NOUN_RE.match(lower):
        logger.info("Meal noun detected → LLM for %r", raw)
        await _run_llm_chat(message, user_id, raw)
        return

    # ── 5. ЯВНЫЕ КОМАНДЫ к ассистенту → LLM, минуя rule-based ──────────────────
    if any(kw in lower for kw in COMMAND_KEYWORDS):
        logger.info("Keyword match → LLM for %r", raw)
        await _run_llm_chat(message, user_id, raw)
        return

    # ── 5.5. КОНТЕКСТНОЕ РЕДАКТИРОВАНИЕ: местоимение или глагол изменения → LLM ──
    # "и поставь ей время на 20:00", "измени её дату", "сделай это на 15:00"
    # Перехватывается ДО rule-based парсера — иначе создаётся фантомная задача.
    if _CONTEXT_EDIT_RE.search(lower):
        logger.info("Context edit detected → LLM for %r", raw)
        await _run_llm_chat(message, user_id, raw)
        return

    # ── 5.7. ГАРДА ЧТЕНИЯ: просмотр/чтение данных → никогда не создавать задачу ──
    # "что записалось", "прочитай лог", "куда попала запись" и т.п.
    # Ставится последней перед rule-based парсером — финальный барьер.
    if _READ_REQUEST_RE.search(lower):
        logger.info("Read request detected → LLM for %r", raw)
        await _run_llm_chat(message, user_id, raw)
        return

    # ── 6. RULE-BASED ПАРСЕР / FALLBACK → LLM ───────────────────────────────────
    try:
        parsed_list: list[ParseResult] = await asyncio.to_thread(parse_task, raw)
    except ValueError:
        # Дата не распознана → LLM tool-calling
        await _run_llm_chat(message, user_id, raw)
        return
    except Exception:
        logger.exception("parse_task failed for %r", raw)
        await message.answer("⚠️ Не смог разобрать задачу. Попробуй: завтра в 15:00 встреча")
        return

    messages_to_send: list[tuple] = []
    with SessionLocal() as session:
        svc = TaskService(session)
        repo = TaskRepo(session)
        for parsed_item in parsed_list:
            task_id = svc.create_task(parsed_item, user_id)
            # Подтверждаем сразу и синхронизируем с Google Calendar —
            # кнопка confirm больше не нужна.
            svc.confirm_and_sync(task_id)
            task = repo.get(task_id)
            if task is not None:
                messages_to_send.append((_build_card(task), _build_keyboard(task_id)))

    if not messages_to_send:
        await message.answer("⚠️ Не удалось создать задачу.")
        return

    for card, kb in messages_to_send:
        await message.answer("✅ " + card, reply_markup=kb)


# ---------------------------------------------------------------------------
# Прямая запись в Obsidian-лог (без LLM-раунда)
# ---------------------------------------------------------------------------

async def _run_log_direct(message: Message, user_id: int, sphere: str, entry: str) -> None:
    try:
        result = await append_obsidian_log(sphere, entry)
        logger.info("Direct log (sphere=%s) result: %s", sphere, result[:120])
        if result.startswith("Ошибка"):
            await message.answer(result)
        else:
            _set_log_context(user_id, _resolve_sphere(sphere))  # нормализуем перед сохранением
            await message.answer(f"📝 Добавлено в {_resolve_sphere(sphere)}.")
    except Exception:
        logger.exception("Direct log failed for user=%s sphere=%s", user_id, sphere)
        await message.answer("⚠️ Не смог записать. Попробуй позже.")


# ---------------------------------------------------------------------------
# Next-step engine handler
# ---------------------------------------------------------------------------

async def _run_cant_sleep(message: Message, user_id: int) -> None:
    try:
        from aiogram.enums import ChatAction
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        from app.llm.obsidian_tools import append_to_bot_log
        await append_to_bot_log("health.md", "Не спится")
        with SessionLocal() as session:
            from app.domain.next_step import suggest_light_activity
            text = await suggest_light_activity(session, user_id)
        await message.answer(f"📝 Записал в health.md.\n\n🎯 {text}")
    except Exception:
        logger.exception("cant_sleep handler failed for user=%s", user_id)
        await message.answer("⚠️ Не смог обработать запрос.")


async def _run_next_step(message: Message, user_id: int) -> None:
    try:
        from aiogram.enums import ChatAction
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        with SessionLocal() as session:
            from app.domain.next_step import suggest_next_step
            text = await suggest_next_step(session, user_id)
        await message.answer(f"🎯 {text}")
    except Exception:
        logger.exception("next_step failed for user=%s", user_id)
        await message.answer("⚠️ Не смог проанализировать. Попробуй позже.")


# ---------------------------------------------------------------------------
# LLM tool-calling цикл
# ---------------------------------------------------------------------------

async def _run_llm_chat(message: Message, user_id: int, raw: str) -> None:
    reply: str | None = None
    try:
        with SessionLocal() as session:
            from app.storage.dialog_repo import DialogRepo
            dialog_repo = DialogRepo(session)

            # Ленивая очистка устаревших сообщений (TTL=24h) для этого пользователя
            dialog_repo.purge_old(user_id)

            # build_messages возвращает [system_msg, user_msg]
            base = build_messages(session, user_id, raw)
            system_msg = base[0]        # {"role": "system", "content": "..."}
            current_user_msg = base[-1]  # {"role": "user",   "content": raw}

            # Инъекция истории из SQLite между системным промптом и текущим сообщением
            msgs = [system_msg] + dialog_repo.get_recent(user_id) + [current_user_msg]

            # Первый вызов: с инструментами
            response_msg = await asyncio.to_thread(call_deepseek_chat, msgs, TOOLS)
            tool_calls = response_msg.get("tool_calls") or []

            if tool_calls:
                msgs.append({
                    "role": "assistant",
                    "content": response_msg.get("content"),
                    "tool_calls": tool_calls,
                })

                created_task_ids: list[int] = []

                for tc in tool_calls:
                    result = await execute_tool_call(tc, session, user_id)
                    logger.info("Tool %r → %s", tc["function"]["name"], result[:120])

                    # Запоминаем сферу после успешной записи лога — для продолжений
                    if tc["function"]["name"] == "append_obsidian_log" and not result.startswith("Ошибка"):
                        try:
                            args = json.loads(tc["function"]["arguments"])
                            resolved = _resolve_sphere(args.get("sphere", ""))
                            if resolved:
                                _set_log_context(user_id, resolved)
                                logger.info("Log context set: user=%s sphere=%s", user_id, resolved)
                        except (json.JSONDecodeError, KeyError):
                            pass

                    # Отслеживаем созданные задачи — покажем карточку с кнопками
                    if tc["function"]["name"] == "create_task":
                        try:
                            parsed_result = json.loads(result)
                            if parsed_result.get("ok"):
                                m = re.search(r"\bid=(\d+)\b", parsed_result.get("result", ""))
                                if m:
                                    created_task_ids.append(int(m.group(1)))
                        except (json.JSONDecodeError, AttributeError):
                            pass

                    msgs.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })

                # Собираем карточки для созданных задач пока сессия открыта
                task_cards: list[tuple] = []
                for tid in created_task_ids:
                    task = TaskRepo(session).get(tid)
                    if task is not None:
                        task_cards.append((_build_card(task), _build_keyboard(tid)))

                # Финальный вызов без инструментов — получаем текстовый ответ
                final_msg = await asyncio.to_thread(call_deepseek_chat, msgs)
                reply = (final_msg.get("content") or "").strip() or "Готово."
            else:
                task_cards = []
                reply = (response_msg.get("content") or "").strip() or "Понял."

            session.commit()

    except Exception:
        logger.exception("LLM chat failed for user=%s text=%r", user_id, raw)
        await message.answer("⚠️ Не смог обработать запрос. Попробуй позже.")
        return

    reply = _clean_reply(reply) or "Готово."

    # Сохраняем ход в SQLite (очищенный ответ — тот, что получил пользователь)
    try:
        with SessionLocal() as hist_session:
            from app.storage.dialog_repo import DialogRepo
            repo = DialogRepo(hist_session)
            repo.append(user_id, "user", raw)
            repo.append(user_id, "assistant", reply)
            hist_session.commit()
    except Exception:
        logger.warning("Dialog history write failed for user=%s", user_id)

    try:
        await message.answer(reply)
    except TelegramBadRequest as exc:
        err = str(exc).lower()
        if "can't parse entities" in err or "unsupported start tag" in err:
            logger.warning("Telegram rejected markup, retrying as plain text: %s", exc)
            await message.answer(reply, parse_mode=None)
        else:
            raise

    # Карточки с кнопками для задач, созданных через LLM tool create_task
    for card, kb in task_cards:
        try:
            await message.answer(card, reply_markup=kb)
        except Exception:
            logger.warning("Failed to send task card after LLM create_task")
