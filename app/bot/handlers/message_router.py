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
from app.bot.handlers.memory import send_memory_proposal
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

# Теги, которые DeepSeek иногда «протекает» в финальный content:
# <｜tool_calls｜>, <｜dsml｜tool_calls｜> и подобные.
# Fullwidth vertical line U+FF5C (｜) — характерный маркер.
_DEEPSEEK_TAG_RE = re.compile(r"</?｜[^>]*>", re.IGNORECASE)


def _clean_reply(text: str) -> str:
    return _DEEPSEEK_TAG_RE.sub("", text).strip()


main_router = Router(name="main")

# Порядок важен: commands → callbacks/FSM → text dispatch (этот модуль)
main_router.include_router(commands_router)
main_router.include_router(tasks_router)


# ---------------------------------------------------------------------------
# Текстовый диспетчер: rule-based → LLM tool-calling
# ---------------------------------------------------------------------------

# Фразы, которые явно адресованы ассистенту, а не являются записью задачи.
# При совпадении пропускаем rule-based парсер и идём сразу в LLM.
COMMAND_KEYWORDS = [
    "что у меня",
    "покажи",
    "перенес",
    "перенеси",
    "сделал",
    "выполнил",
    "удали",
    "удалить",
    "добавь задачу",
    "поставь задачу",
    "напомни",
    "отложи",
    "какие задачи",
    "план на", "перенес", "перенеси", "сделал", "выполнил",
    "удали", "удалить", "отмени", "покажи", "добавь задачу",
    "поставь задачу", "какие задачи", "план на", "запомни",
]

# ---------------------------------------------------------------------------
# Obsidian-лог: детектор намерения «записать факт в сферу»
# ---------------------------------------------------------------------------

# Ловим оба порядка слов:
#   прямой:   "Запиши в питание: ..."   → group sphere1
#   обратный: "В питание запиши: ..."   → group sphere2
_OBSIDIAN_LOG_RE = re.compile(
    r"^(?:"
    r"(?:запиши|запишите|добавь|добавьте|отметь|отметьте|зафиксируй|зафиксируйте|залогируй|залогируйте)"
    r"\s+в\s+(?P<sphere1>[^\s:]+)"
    r"|"
    r"в\s+(?P<sphere2>[^\s:]+)\s+(?:запиши|запишите|добавь|добавьте|отметь|отметьте|зафиксируй|зафиксируйте|залогируй|залогируйте)"
    r")",
    re.IGNORECASE,
)

# Известные названия сфер — используются как второй уровень проверки,
# если глагол есть, но за ним не сразу "в слово" (нестандартный порядок слов).
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


def _extract_log_sphere(m: re.Match) -> str | None:
    """Возвращает очищенное имя сферы из матча _OBSIDIAN_LOG_RE."""
    raw = m.group("sphere1") or m.group("sphere2") or ""
    cleaned = re.sub(r"[^\w]", "", raw, flags=re.UNICODE).lower()
    return cleaned or None


def _is_obsidian_log_intent(lower: str) -> bool:
    """True если сообщение — запись-в-сферу, а не постановка будущей задачи."""
    if _OBSIDIAN_LOG_RE.match(lower):
        return True
    # Запасной вариант: явная сфера + глагол-маркер в любом месте текста
    first_word = lower.split()[0] if lower.split() else ""
    has_sphere = any(kw in lower for kw in _LOG_SPHERE_KEYWORDS)
    has_verb = any(v in lower for v in _LOG_LEADING_VERBS)
    if first_word == "в":
        # "в питание добавь ...", "в дневник отметь ..."
        return has_sphere and has_verb
    return first_word in _LOG_LEADING_VERBS and has_sphere


@main_router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def dispatch_text(message: Message) -> None:
    raw = message.text.strip()
    user_id = message.from_user.id if message.from_user else 0
    lower = raw.lower()

    # — Obsidian-лог: явная сфера в сообщении → LLM (он вызовет append_obsidian_log и обновит кэш) —
    if _is_obsidian_log_intent(lower):
        m = _OBSIDIAN_LOG_RE.match(lower)
        sphere = _extract_log_sphere(m) if m else None
        logger.info("Obsidian log intent (sphere=%s) → LLM for %r", sphere, raw)
        await _run_llm_chat(message, user_id, raw)
        return

    # — Продолжение лога: "еще/ещё/также/плюс/добавь" + свежий кэш → прямой вызов без LLM —
    continuation_sphere = _check_log_continuation(user_id, lower)
    if continuation_sphere is not None:
        # Убираем вводное слово для чистоты записи: "еще съел 300г" → "съел 300г"
        entry = _LOG_CONTINUATION_RE.sub("", raw, count=1).lstrip(":, ").strip() or raw
        logger.info("Log continuation (sphere=%s) → direct append for %r", continuation_sphere, raw)
        await _run_log_direct(message, user_id, continuation_sphere, entry)
        return

    # — Явные команды → сразу LLM, минуя rule-based —
    if any(kw in lower for kw in COMMAND_KEYWORDS):
        logger.info("Keyword match → LLM for %r", raw)
        await _run_llm_chat(message, user_id, raw)
        return

    # — Rule-based / LLM parsing path —
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
            task = repo.get(task_id)
            if task is not None:
                card = _build_card(task, parser=parsed_item.parser)
                kb = _build_keyboard(task_id)
                messages_to_send.append((card, kb))

    if not messages_to_send:
        await message.answer("⚠️ Задача записана, но не удалось её прочитать.")
        return

    for card, kb in messages_to_send:
        await message.answer(card, reply_markup=kb)


# ---------------------------------------------------------------------------
# Прямая запись в Obsidian-лог (для продолжений, без LLM-раунда)
# ---------------------------------------------------------------------------

async def _run_log_direct(message: Message, user_id: int, sphere: str, entry: str) -> None:
    try:
        result = await append_obsidian_log(sphere, entry)
        logger.info("Direct log (sphere=%s) result: %s", sphere, result[:120])
        if result.startswith("Ошибка"):
            await message.answer(result)
        else:
            _set_log_context(user_id, sphere)  # обновляем TTL для следующего продолжения
            await message.answer(f"📝 Добавлено в {sphere}.")
    except Exception:
        logger.exception("Direct log failed for user=%s sphere=%s", user_id, sphere)
        await message.answer("⚠️ Не смог записать. Попробуй позже.")


# ---------------------------------------------------------------------------
# LLM tool-calling цикл
# ---------------------------------------------------------------------------

async def _run_llm_chat(message: Message, user_id: int, raw: str) -> None:
    reply: str | None = None
    try:
        with SessionLocal() as session:
            msgs = build_messages(session, user_id, raw)

            # Первый вызов: с инструментами
            response_msg = await asyncio.to_thread(call_deepseek_chat, msgs, TOOLS)
            tool_calls = response_msg.get("tool_calls") or []

            if tool_calls:
                # Добавляем ответ ассистента с tool_calls в историю
                msgs.append({
                    "role": "assistant",
                    "content": response_msg.get("content"),
                    "tool_calls": tool_calls,
                })

                # Выполняем все инструменты
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

                    # Side effect: предложить сохранить память
                    if tc["function"]["name"] == "propose_memory_save":
                        try:
                            outer = json.loads(result)
                            if outer.get("ok"):
                                inner = json.loads(outer["result"])
                                await send_memory_proposal(
                                    message.bot,
                                    user_id,
                                    inner["memory_id"],
                                    inner["content"],
                                    inner["memory_type"],
                                )
                        except (json.JSONDecodeError, KeyError):
                            logger.warning("Could not parse propose_memory_save result")

                    msgs.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })

                # Финальный вызов без инструментов — получаем текстовый ответ
                final_msg = await asyncio.to_thread(call_deepseek_chat, msgs)
                reply = (final_msg.get("content") or "").strip() or "Готово."
            else:
                # LLM ответил текстом без вызова инструментов
                reply = (response_msg.get("content") or "").strip() or "Понял."

            session.commit()

    except Exception:
        logger.exception("LLM chat failed for user=%s text=%r", user_id, raw)
        await message.answer("⚠️ Не смог обработать запрос. Попробуй позже.")
        return

    reply = _clean_reply(reply) or "Готово."

    try:
        await message.answer(reply)
    except TelegramBadRequest as exc:
        err = str(exc).lower()
        if "can't parse entities" in err or "unsupported start tag" in err:
            logger.warning("Telegram rejected markup, retrying as plain text: %s", exc)
            await message.answer(reply, parse_mode=None)
        else:
            raise
