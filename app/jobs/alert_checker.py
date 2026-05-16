"""Proactive alert checker.

Runs daily at 20:30. Checks all spheres for red flags, composes
a short LLM message with one concrete next step, sends to user.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.config import settings
from app.domain.alert_rules import Alert, run_all_checks
from app.storage.db import SessionLocal

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
Ты личный AI-коуч. Сейчас вечер. Пользователь сам не спрашивал — ты пишешь первым.

Твой стиль:
- Коротко и по делу (2-4 предложения максимум)
- Не занудный, не морализаторский
- Один конкретный следующий шаг (не список)
- Учитывай, что человек устал за день

Не начинай с "Привет" или "Добрый вечер". Сразу к делу.\
"""


def _build_prompt(alerts: list[Alert], state=None) -> str:
    warnings = [a for a in alerts if a.severity == "warning"]
    infos    = [a for a in alerts if a.severity == "info"]

    lines = ["Сработали следующие алёрты (данные системы, не интерпретируй буквально):"]
    if warnings:
        lines.append("\n🔴 Требует внимания:")
        for a in warnings:
            lines.append(f"  [{a.sphere}] {a.summary}")
    if infos:
        lines.append("\n🟡 Обрати внимание:")
        for a in infos:
            lines.append(f"  [{a.sphere}] {a.summary}")

    if state and state.energy_source != "unknown":
        h, m = divmod(state.sleep_min or 0, 60)
        sleep_str = f", сон прошлой ночью {h}ч {m}м" if state.sleep_min else ""
        lines.append(
            f"\nТекущее состояние: энергия {state.energy}/10 — {state.energy_label}"
            f"{sleep_str}."
        )
        if state.energy and state.energy <= 4:
            lines.append(
                "Человек устал. Не предлагай интенсивные тренировки или сложную работу. "
                "Лучший шаг — восстановление (сон, лёгкая прогулка, 15 мин румынского)."
            )

    lines.append(
        "\nНапиши пользователю короткое сообщение. "
        "Выдели главное (один красный флажок если есть, иначе самый важный жёлтый). "
        "Предложи один конкретный шаг который реально можно сделать прямо сейчас или перед сном."
    )
    return "\n".join(lines)


def _call_llm(alerts: list[Alert], state=None) -> str | None:
    prompt = _build_prompt(alerts, state)
    try:
        from app.llm.deepseek_client import call_deepseek_chat
        msg = call_deepseek_chat(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ]
        )
        return (msg.get("content") or "").strip() or None
    except Exception as exc:
        logger.error("Alert LLM call failed: %s", exc)
        return None


def _fallback_message(alerts: list[Alert]) -> str:
    warnings = [a for a in alerts if a.severity == "warning"]
    focus = warnings[0] if warnings else alerts[0]
    sphere_emoji = {
        "nutrition": "🍽", "training": "💪", "sleep": "🌙",
        "german": "📖", "romanian": "🇷🇴",
    }
    icon = sphere_emoji.get(focus.sphere, "⚠️")
    return f"{icon} {focus.summary}"


async def check_proactive_alerts(bot: Bot) -> None:
    user_id = settings.allowed_user_id
    if user_id is None:
        logger.warning("check_proactive_alerts: allowed_user_id not set, skipping")
        return

    with SessionLocal() as session:
        alerts = run_all_checks(session, user_id)
        from app.domain.state import get_current_state
        state = get_current_state(session, user_id)

    if not alerts:
        logger.debug("No alerts today, skipping message")
        return

    from app.domain.alert_throttle import filter_alerts, mark_alerts_sent
    alerts = filter_alerts(alerts)

    if not alerts:
        logger.debug("All alerts throttled, skipping message")
        return

    logger.info(
        "Alerts fired: %s (energy=%s)",
        [f"{a.key}({a.severity})" for a in alerts],
        state.energy,
    )

    import asyncio
    text = await asyncio.to_thread(_call_llm, alerts, state)
    if not text:
        text = _fallback_message(alerts)

    await bot.send_message(user_id, text)
    mark_alerts_sent(alerts)
    logger.info("Proactive alert sent to user=%s", user_id)
