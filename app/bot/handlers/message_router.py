import asyncio
import json
import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.types import Message

from app.bot.handlers.commands import commands_router
from app.bot.handlers.memory import send_memory_proposal
from app.bot.handlers.tasks import _build_card, _build_keyboard, tasks_router
from app.domain.task_service import TaskService
from app.llm.deepseek_client import call_deepseek_chat
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
    "план на","что у меня", "перенес", "перенеси", "сделал", "выполнил",
    "удали", "удалить", "отмени", "покажи", "добавь задачу",
    "поставь задачу", "какие задачи", "план на", "запомни"
]


@main_router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def dispatch_text(message: Message) -> None:
    raw = message.text.strip()
    user_id = message.from_user.id if message.from_user else 0
    lower = raw.lower()

    # — Явные команды → сразу LLM, минуя rule-based —
    if any(kw in lower for kw in COMMAND_KEYWORDS):
        logger.info("Keyword match → LLM for %r", raw)
        await _run_llm_chat(message, user_id, raw)
        return

    # — Rule-based path —
    try:
        parsed: ParseResult = await asyncio.to_thread(parse_task, raw)
    except ValueError:
        # Дата не распознана → LLM
        await _run_llm_chat(message, user_id, raw)
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
                    result = execute_tool_call(tc, session, user_id)
                    logger.info("Tool %r → %s", tc["function"]["name"], result[:120])

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
