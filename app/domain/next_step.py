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

ПРИНЦИПЫ ИВАНА (всегда применять):
- Иван всегда должен учиться и двигаться вперёд.
- Отдых легитимен ТОЛЬКО если он целенаправленный (восстановление ради энергии).
- Лень ≠ отдых. "Просто полежать", скроллинг — не рекомендации.

ПРАВИЛА:
- Ровно одно действие, не список.
- Конкретное: не «займись румынским», а «открой Duolingo — сделай один урок (10 мин)».
- Учитывай доступное время: окно < 20 мин → только короткое; > 60 мин → можно серьёзное.
- Используй Energy Matrix: подбирай активность под уровень энергии и тип последней нагрузки.
- Соблюдай Planning Principles и Frequency Constraints из матрицы.
- Приоритет: красные алёрты > якоря дня > задачи с дедлайном > Priority Weights.
- Проверь Project Files: нет Next Actions → 15-мин сессия планирования ("Открой project_X.md").
- Если в Next Actions есть [ ] пункты → предлагай первый незакрытый.
- Энергия неизвестна → Medium рекомендация + в конце: "Оцени энергию 1-10 — дам точнее."

ВОССТАНОВЛЕНИЕ (энергия ≤ 3) — иерархия по "Ещё не было сегодня":
1. Сон (slept=False) → вздремни 20 мин или ляг раньше
2. Еда (ate=False) → поешь нормально прямо сейчас
3. Прогулка (walked=False или нет training-записи с "прогулк"/"walk") → 20–30 мин на воздухе
4. Всё выше есть → 15–20 мин гитары / лёгкое чтение / музыка без экрана
5. В конце: "Договорились? Скажи как самочувствие через полчаса."

ОТВЕТ: 2-3 предложения. Без вступлений, без заголовков.\
"""

_DAY_PLAN_SYSTEM = """\
Ты личный планировщик. Составь план оставшегося дня по временным блокам.
Учитывай: текущее время, энергию, что уже сделано сегодня, задачи с дедлайнами,
проекты с незакрытыми Next Actions, свободные окна в календаре.
Принципы Ивана: всегда учиться/двигаться; отдых только целенаправленный.
Формат:
  HH:MM — Активность (продолжительность)
  HH:MM — ...
  Вечер — лёгкое завершение дня (если энергия упадёт)
Заверши одной строкой: "Такой план устраивает? Могу скорректировать."\
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


def _get_today_activity(session: Session, user_id: int) -> dict[str, bool]:
    """Какие сферы имеют записи СЕГОДНЯ в log_entries."""
    from app.storage.log_repo import LogRepo
    tz = ZoneInfo(settings.task_timezone)
    today = datetime.now(tz=tz).strftime("%Y-%m-%d")
    entries = LogRepo(session).get_recent(user_id, days=1, limit=100)
    today_entries = [e for e in entries if e.logged_at.startswith(today)]
    spheres = {e.sphere for e in today_entries}
    training_entries = [e for e in today_entries if e.sphere == "training"]
    walked = any(
        "прогулк" in e.raw_text.lower() or "walk" in e.raw_text.lower()
        for e in training_entries
    )
    return {
        "trained":  "training"  in spheres,
        "slept":    "sleep"     in spheres,
        "ate":      "nutrition" in spheres,
        "german":   "german"    in spheres,
        "romanian": "romanian"  in spheres,
        "walked":   walked,
        "guitar":   "guitar"    in spheres,
    }


_ACTIVITY_LABELS: list[tuple[str, str]] = [
    ("slept",    "сон"),
    ("ate",      "питание"),
    ("trained",  "тренировка"),
    ("german",   "немецкий"),
    ("romanian", "румынский"),
    ("guitar",   "🎸 гитара"),
]


def _build_context_prompt(ctx: dict) -> str:
    lines = [
        f"Текущий момент: {ctx['now_str']}",
    ]

    # Energy
    if ctx["energy"] is None:
        lines.append("Энергия: неизвестна (спроси пользователя оценить 1-10 в конце ответа)")
    else:
        src = {"explicit": "пинг", "inferred": "инференс по сну"}.get(ctx["energy_source"], "")
        e_str = f"{ctx['energy']}/10 — {ctx['energy_label']}"
        if src:
            e_str += f" ({src})"
        if ctx["sleep_min"]:
            h, m = divmod(ctx["sleep_min"], 60)
            e_str += f"; сон {h}ч {m}м"
        lines.append(f"Энергия: {e_str}")

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

    # Backlog
    if ctx.get("backlog"):
        lines.append(f"Бэклог (без даты, {len(ctx['backlog'])} шт.) — если есть свободное окно:")
        for b in ctx["backlog"][:3]:
            lines.append(f"  {b}")
        if len(ctx["backlog"]) > 3:
            lines.append(f"  … ещё {len(ctx['backlog']) - 3}")

    # Today's activity
    act = ctx.get("today_activity")
    if act is not None:
        _OPTIONAL = {"guitar"}  # показываем в "сделано", но не в "не было"
        done = [lbl for key, lbl in _ACTIVITY_LABELS if act.get(key)]
        if act.get("walked") and "тренировка" not in done:
            done.append("прогулка")
        not_done = [lbl for key, lbl in _ACTIVITY_LABELS if not act.get(key) and key not in _OPTIONAL]
        lines.append(f"Сделано сегодня: {', '.join(done) if done else 'ничего'}")
        if not_done:
            lines.append(f"Ещё не было сегодня: {', '.join(not_done)}")
        if act.get("trained") and not act.get("walked"):
            lines.append("  (тренировка была, но прогулки не было)")

    # Alerts
    if ctx["alerts"]:
        lines.append("Активные алёрты (красные):")
        for a in ctx["alerts"]:
            lines.append(f"  ⚠️ {a}")

    # Project Files
    projects = ctx.get("project_context", "")
    if projects:
        lines.append(f"\n--- Project Files ---\n{projects}\n--- End Projects ---")

    # Energy Matrix
    matrix = ctx.get("energy_matrix", "")
    if matrix:
        lines.append(f"\n--- Energy Matrix ---\n{matrix}\n--- End Matrix ---")

    lines.append(
        "\nВыбери ОДИН следующий шаг с учётом всего выше. "
        "Обоснуй выбор одним предложением."
    )
    return "\n".join(lines)


async def _gather_context(session: Session, user_id: int) -> dict:
    from app.domain.alert_rules import run_all_checks
    from app.domain.state import get_current_state
    from app.llm.obsidian_tools import read_energy_matrix_sync
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
    energy_matrix = read_energy_matrix_sync()
    from app.llm.obsidian_tools import read_project_files_sync
    project_context = read_project_files_sync()
    backlog = TaskRepo(session).get_backlog_tasks(user_id, limit=5)
    backlog_lines = [f"• id={t.id} {t.text}" for t in backlog]
    today_activity = _get_today_activity(session, user_id)

    return {
        "now_str": now_str,
        "energy": state.energy,
        "energy_label": state.energy_label,
        "energy_source": state.energy_source,
        "sleep_min": state.sleep_min,
        "tasks": task_lines,
        "backlog": backlog_lines,
        "alerts": [a.summary for a in alerts],
        "free_window_min": free_window_min,
        "next_event_str": next_event_str,
        "energy_matrix": energy_matrix,
        "project_context": project_context,
        "today_activity": today_activity,
    }


def _call_llm_with_system(system: str, user_prompt: str) -> str | None:
    try:
        from app.llm.deepseek_client import call_deepseek_chat
        msg = call_deepseek_chat(messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user_prompt},
        ])
        return (msg.get("content") or "").strip() or None
    except Exception as exc:
        logger.error("next_step LLM call failed: %s", exc)
        return None


def _call_llm(user_prompt: str) -> str | None:
    return _call_llm_with_system(_SYSTEM_PROMPT, user_prompt)


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


async def suggest_day_plan(session: Session, user_id: int) -> str:
    """Return a full day plan as a formatted string."""
    ctx = await _gather_context(session, user_id)
    prompt = _build_context_prompt(ctx)
    text = await asyncio.to_thread(_call_llm_with_system, _DAY_PLAN_SYSTEM, prompt)
    return text or "Не смог составить план. Попробуй позже."


async def suggest_light_activity(session: Session, user_id: int) -> str:
    """Light recommendation for when user can't sleep — ignores late-night gate."""
    from app.domain.state import get_current_state
    state = get_current_state(session, user_id)

    prompt = _CANT_SLEEP_PROMPT
    if state.energy:
        prompt += f" Текущая энергия пользователя: {state.energy}/10."

    text = await asyncio.to_thread(_call_llm, prompt)
    return text or "Попробуй послушать немецкий подкаст или сделай 5 минут дыхательного упражнения — без экрана если возможно."
