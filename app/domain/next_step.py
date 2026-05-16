"""Next-step engine: gather full context → LLM → one concrete action.

Used by:
  - Scheduled midday push (12:30)
  - On-demand ("что мне сейчас делать?")
"""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)

_WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг",
                "пятница", "суббота", "воскресенье"]

# After this time (23:40) the only right step is sleep.
_SLEEP_HOUR = 23
_SLEEP_MINUTE = 40

_SLEEP_MESSAGE = (
    "Уже после 23:40 — лучший следующий шаг это лечь спать. "
    "Хороший сон важнее любой задачи. Если не спится — скажи об этом."
)

_CANT_SLEEP_PROMPT = """\
Пользователь не может заснуть. Сейчас ночь. Предложи ОДНО лёгкое занятие \
которое поможет расслабиться или использовать время без стресса. \
Не работу, не тренировку — только лёгкое: подкаст на немецком/румынском, \
дыхательное упражнение, лёгкое чтение, медитация, растяжка. \
Конкретно и коротко — 2 предложения.\
"""


def _is_late_night(now: datetime) -> bool:
    h, m = now.hour, now.minute
    return (h == _SLEEP_HOUR and m >= _SLEEP_MINUTE) or (0 <= h < 5)

_SYSTEM_PROMPT = """\
Ты личный AI-коуч. Твоя задача — дать ОДИН конкретный следующий шаг.

Правила:
- Ровно одно действие, не список.
- Конкретное: не «займись румынским», а «открой Duolingo — сделай один урок (10 мин)».
- Учитывай доступное время: если окно < 20 мин — только короткое; > 60 мин — можно серьёзное.
- Учитывай энергию: при ≤4 — только лёгкое (язык, прогулка); при ≥8 — можно demanding.
- Приоритет: красные алёрты > задачи с дедлайном сегодня > языки > всё остальное.
- Ответ: 2-3 предложения. Без вступлений, без заголовков.\
"""


def _compute_free_window(cal_events, now: datetime) -> tuple[int, str | None]:
    """Return (free_minutes, 'EventName в HH:MM') or (240, None) if no upcoming events."""
    upcoming = [
        e for e in cal_events
        if not e.all_day and not e.is_bot_task
    ]
    for e in sorted(upcoming, key=lambda x: x.start_time):
        try:
            h, m = (int(x) for x in e.start_time.split(":"))
            event_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if event_dt > now:
                free_min = int((event_dt - now).total_seconds() / 60)
                return free_min, f"{e.summary} в {e.start_time}"
        except ValueError:
            continue
    return 240, None


def _build_context_prompt(ctx: dict) -> str:
    lines = [
        f"Текущий момент: {ctx['now_str']}",
    ]

    # Energy
    if ctx["energy"] is not None:
        src = {"explicit": "пинг", "inferred": "инференс по сну"}.get(ctx["energy_source"], "")
        e_str = f"{ctx['energy']}/10 — {ctx['energy_label']}"
        if src:
            e_str += f" ({src})"
        if ctx["sleep_min"]:
            h, m = divmod(ctx["sleep_min"], 60)
            e_str += f"; сон {h}ч {m}м"
        lines.append(f"Энергия: {e_str}")
    else:
        lines.append("Энергия: неизвестна")

    # Free window
    fw = ctx["free_window_min"]
    nxt = ctx["next_event_str"]
    if nxt:
        lines.append(f"Свободное окно: ~{fw} мин (потом {nxt})")
    else:
        lines.append(f"Свободное окно: ~{fw} мин (встреч не обнаружено)")

    # Tasks
    if ctx["tasks"]:
        lines.append(f"Задачи на сегодня ({len(ctx['tasks'])}):")
        for t in ctx["tasks"][:5]:
            lines.append(f"  • {t}")
        if len(ctx["tasks"]) > 5:
            lines.append(f"  … ещё {len(ctx['tasks']) - 5}")
    else:
        lines.append("Задачи на сегодня: нет")

    # Alerts
    if ctx["alerts"]:
        lines.append("Активные алёрты (красные):")
        for a in ctx["alerts"]:
            lines.append(f"  ⚠️ {a}")

    lines.append(
        "\nВыбери ОДИН следующий шаг с учётом всего выше. "
        "Обоснуй выбор одним предложением."
    )
    return "\n".join(lines)


async def _gather_context(session: Session, user_id: int) -> dict:
    from app.domain.alert_rules import run_all_checks
    from app.domain.state import get_current_state
    from app.storage.task_repo import TaskRepo

    tz = ZoneInfo(settings.task_timezone)
    now = datetime.now(tz=tz)
    today = now.strftime("%Y-%m-%d")
    now_str = f"{now.strftime('%H:%M')} ({_WEEKDAYS_RU[now.weekday()]})"

    state = get_current_state(session, user_id)
    alerts = [a for a in run_all_checks(session, user_id) if a.severity == "warning"]
    tasks = TaskRepo(session).get_today_plan(user_id, today=today)
    task_lines = [
        f"{t.event_time or 'весь день'} — {t.text}" for t in tasks
    ]

    cal_events = []
    try:
        from app.domain.google_calendar import get_upcoming_events
        cal_events = await asyncio.to_thread(get_upcoming_events, today, 1)
    except Exception as exc:
        logger.warning("next_step: GCal fetch failed: %s", exc)

    free_window_min, next_event_str = _compute_free_window(cal_events, now)

    return {
        "now_str": now_str,
        "energy": state.energy,
        "energy_label": state.energy_label,
        "energy_source": state.energy_source,
        "sleep_min": state.sleep_min,
        "tasks": task_lines,
        "alerts": [a.summary for a in alerts],
        "free_window_min": free_window_min,
        "next_event_str": next_event_str,
    }


def _call_llm(user_prompt: str) -> str | None:
    try:
        from app.llm.deepseek_client import call_deepseek_chat
        msg = call_deepseek_chat(messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ])
        return (msg.get("content") or "").strip() or None
    except Exception as exc:
        logger.error("next_step LLM call failed: %s", exc)
        return None


async def suggest_next_step(session: Session, user_id: int) -> str:
    """Return ONE concrete next-step suggestion as a string."""
    tz = ZoneInfo(settings.task_timezone)
    now = datetime.now(tz=tz)

    if _is_late_night(now):
        return _SLEEP_MESSAGE

    ctx = await _gather_context(session, user_id)
    prompt = _build_context_prompt(ctx)
    text = await asyncio.to_thread(_call_llm, prompt)

    if not text:
        if ctx["alerts"]:
            return f"⚠️ Обрати внимание: {ctx['alerts'][0]}"
        if ctx["tasks"]:
            return f"Следующая задача: {ctx['tasks'][0]}"
        return "Похоже, сейчас нет срочных дел. Хорошее время для языков или отдыха."

    return text


async def suggest_light_activity(session: Session, user_id: int) -> str:
    """Light recommendation for when user can't sleep — ignores late-night gate."""
    from app.domain.state import get_current_state
    state = get_current_state(session, user_id)

    prompt = _CANT_SLEEP_PROMPT
    if state.energy:
        prompt += f" Текущая энергия пользователя: {state.energy}/10."

    text = await asyncio.to_thread(_call_llm, prompt)
    return text or "Попробуй послушать немецкий подкаст или сделай 5 минут дыхательного упражнения — без экрана если возможно."
