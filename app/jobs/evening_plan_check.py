import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import yaml

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.storage.db import SessionLocal
from app.storage.task_repo import TaskRepo

logger = logging.getLogger(__name__)


def _load_daily_goals() -> list[dict]:
    """Read daily_goals from protocols.yaml. Falls back to empty list."""
    path = Path(settings.obsidian_vault_path) / "_bot" / "protocols.yaml"
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("daily_goals", [])
    except Exception as exc:
        logger.warning("Could not load daily_goals from protocols.yaml: %s", exc)
        return []


async def send_language_nudge(bot: Bot) -> None:
    """19:00 — nudge for any daily_goals not yet logged today."""
    user_id = settings.allowed_user_id
    if not user_id:
        return

    goals = _load_daily_goals()
    if not goals:
        return

    with SessionLocal() as session:
        from app.domain.next_step import _get_today_activity
        activity = _get_today_activity(session, user_id)

    missing = [
        g["label"] for g in goals
        if not activity.get(g["sphere"])
    ]

    if not missing:
        return

    text = "📚 Сегодня ещё не записал:\n" + "\n".join(f"• {m}" for m in missing)
    try:
        await bot.send_message(user_id, text)
        logger.info("Language nudge sent: %s", missing)
    except Exception:
        logger.exception("Failed to send language nudge")


async def send_evening_empty_day(bot: Bot) -> None:
    """21:30 — alert if tomorrow has no tasks planned."""
    user_id = settings.allowed_user_id
    if not user_id:
        return

    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    with SessionLocal() as session:
        tomorrow_tasks = TaskRepo(session).get_today_plan(user_id, today=tomorrow)
        from app.domain.next_step import _get_today_activity
        activity = _get_today_activity(session, user_id)

    if tomorrow_tasks:
        return  # tomorrow already has tasks — no need to alert

    goals = _load_daily_goals()
    missing_goals = [
        g["label"] for g in goals
        if not activity.get(g["sphere"])
    ]

    lines = ["📅 Завтра ещё ничего не запланировано!"]
    if missing_goals:
        lines.append("Сегодня не сделал: " + ", ".join(missing_goals))
    lines.append("\nХочешь спланировать завтра?")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📝 Планировать", callback_data="evening:plan"),
        InlineKeyboardButton(text="⏩ Пропустить",  callback_data="evening:skip"),
    ]])
    try:
        await bot.send_message(user_id, "\n".join(lines), reply_markup=kb)
        logger.info("Evening empty-day alert sent to user=%s", user_id)
    except Exception:
        logger.exception("Failed to send evening empty-day alert")
