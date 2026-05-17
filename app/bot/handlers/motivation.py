"""
Motivation handler.

/motivate [category] — explicit motivational kick on demand.
Passive trigger: refusal phrases + energy >= 5 → auto-motivation.

Reads _bot/motivation.md for category-specific quotes and links.
"""

import asyncio
import logging
import re
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.config import settings

motivation_router = Router(name="motivation")
logger = logging.getLogger(__name__)

_HARSH_SYSTEM_PROMPT = """\
Ты жёсткий личный коуч — честный, прямой, без соплей.
Пользователь при нормальной энергии избегает важного дела.
Дай ему мотивационный пинок в его стиле — он сам попросил жёстче.

Правила:
- 1 предложение: констатируй факт без прикрас (можно с сарказмом)
- 1 предложение: напомни о конкретной цели пользователя
- 1 предложение: чёткий призыв к действию прямо сейчас
- Если в контексте есть цитата или ссылка — добавь после
- Тон: честный, прямой, с юмором — не токсичный
- По-русски, без вступлений типа "Конечно!" или "Понял!"
- Максимум 4 предложения
"""

_CATEGORY_ALIASES: dict[str, str] = {
    "зал": "Зал / Тренировка",
    "gym": "Зал / Тренировка",
    "тренировка": "Зал / Тренировка",
    "training": "Зал / Тренировка",
    "спорт": "Зал / Тренировка",
    "учёба": "Thesis / Deep work",
    "учеба": "Thesis / Deep work",
    "work": "Thesis / Deep work",
    "thesis": "Thesis / Deep work",
    "deep": "Thesis / Deep work",
    "работа": "Thesis / Deep work",
    "немецкий": "Немецкий / Языки",
    "german": "Немецкий / Языки",
    "de": "Немецкий / Языки",
    "румынский": "Румынский",
    "romanian": "Румынский",
    "ro": "Румынский",
    "языки": "Немецкий / Языки",
    "languages": "Немецкий / Языки",
}


def _read_motivation_file() -> str:
    path = Path(settings.obsidian_vault_path) / "_bot" / "motivation.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return ""


def _extract_section(content: str, section_name: str) -> str:
    """Extract content of a ## section from motivation.md."""
    lines = content.splitlines()
    in_section = False
    result: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if section_name.lower() in line.lower():
                in_section = True
                continue
            elif in_section:
                break
        elif in_section:
            result.append(line)
    return "\n".join(result).strip()


def _build_motivation_context(category_key: str | None) -> str:
    content = _read_motivation_file()
    if not content:
        return ""

    if category_key:
        section_name = _CATEGORY_ALIASES.get(category_key.lower(), "Общее")
        section = _extract_section(content, section_name)
        if section:
            return f"Контекст категории «{section_name}»:\n{section}"

    general = _extract_section(content, "Общее")
    return f"Общий контекст:\n{general}" if general else ""


async def _send_motivation(message: Message, category_key: str | None = None) -> None:
    from aiogram.enums import ChatAction
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    context = _build_motivation_context(category_key)
    user_msg = f"Пользователь не хочет заниматься: {category_key or 'важным делом'}."
    if context:
        user_msg = f"{context}\n\n{user_msg}"

    try:
        from app.llm.deepseek_client import call_deepseek_chat
        response = await asyncio.to_thread(call_deepseek_chat, [
            {"role": "system", "content": _HARSH_SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ])
        text = (response.get("content") or "").strip()
    except Exception as exc:
        logger.error("Motivation LLM failed: %s", exc)
        text = ""

    if not text:
        text = "Вставай и делай. Версия себя через 5 лет скажет спасибо — или нет."

    kick = f"💢 {text}"
    await message.answer(kick)

    # Сразу даём конкретный следующий шаг — чтобы не было "ок, а что именно делать?"
    user_id = message.from_user.id if message.from_user else 0
    step_text = ""
    try:
        from app.config import settings as _s
        from app.storage.db import SessionLocal
        from app.domain.next_step import suggest_next_step
        with SessionLocal() as _sess:
            step_text = await suggest_next_step(_sess, user_id)
        await message.answer(f"🎯 {step_text}")
    except Exception as exc:
        logger.warning("next_step after motivate failed: %s", exc)

    # Сохраняем в dialog history чтобы follow-up имел контекст
    try:
        from app.storage.db import SessionLocal
        from app.storage.dialog_repo import DialogRepo
        full_reply = kick + (f"\n\n🎯 {step_text}" if step_text else "")
        with SessionLocal() as _hist:
            repo = DialogRepo(_hist)
            repo.append(user_id, "assistant", full_reply)
            _hist.commit()
    except Exception as exc:
        logger.warning("motivation dialog history write failed: %s", exc)


@motivation_router.message(Command("motivate", "мотивируй", "motivate"))
async def cmd_motivate(message: Message, command: CommandObject) -> None:
    category = (command.args or "").strip().lower() or None
    await _send_motivation(message, category)
