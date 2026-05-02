from datetime import date, datetime
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Parsing prompt (используется task_engine.py)
# ---------------------------------------------------------------------------

def build_task_prompt(text: str, base: date, context: str | None = None) -> str:
    weekdays_ru = [
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье",
    ]
    weekday = weekdays_ru[base.weekday()]

    context_block = f"\nКонтекст пользователя:\n{context}\n" if context else ""

    return f"""Ты парсер задач для Telegram-бота. Верни только JSON без Markdown и пояснений.

Сегодня: {base.isoformat()}.
День недели: {weekday}.
Таймзона пользователя: {settings.task_timezone}.
Язык входа: русский, иногда английский.

Извлеки из текста:
- date: дата задачи в формате YYYY-MM-DD
- time: время в формате HH:MM или null
- all_day: true если конкретного времени нет
- clean_text: текст задачи без слов даты и времени

Правила:
- "вечером" = 19:00
- "утром" = 09:00
- "днём" / "днем" = 14:00
- "ночью" = 22:00
- "в 3 часа" без уточнения = 15:00
- "в пятницу" = ближайшая будущая пятница
- "через N дней" считай от сегодняшней даты
- Если времени нет: time = null, all_day = true
- Если время есть: all_day = false
{context_block}
Вход: {text!r}""".strip()


# ---------------------------------------------------------------------------
# Tool-calling prompts (используются будущим LLM-хендлером)
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(settings.task_timezone))
    weekdays_ru = [
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье",
    ]
    weekday = weekdays_ru[now.weekday()]

    return (
        "Ты AI-ассистент для управления задачами в Telegram-боте.\n"
        f"Сегодня: {now.date().isoformat()}, {weekday}.\n"
        f"Текущее время: {now.strftime('%H:%M')}.\n"
        f"Таймзона: {settings.task_timezone}.\n\n"
        "Ты помогаешь пользователю создавать задачи, отмечать выполненные, "
        "переносить и откладывать дела, смотреть план дня.\n\n"
        "Правила:\n"
        "- Используй инструменты для любых действий с задачами.\n"
        "- Отвечай коротко, по-русски, без лишних слов.\n"
        "- Если намерение неясно — уточни одним вопросом.\n"
        "- Не придумывай task_id. Если нужен ID — сначала вызови get_active_task или get_today_plan."
    )


def build_day_summary(session: "Session", user_id: int) -> str:
    from datetime import date as date_type
    from app.storage.task_repo import TaskRepo

    repo = TaskRepo(session)
    today_str = date_type.today().isoformat()
    today_tasks = repo.get_today_plan(user_id, today=today_str)
    active = repo.get_active_task(user_id)

    if not today_tasks and active is None:
        return ""

    lines = ["Задачи пользователя:"]

    if today_tasks:
        lines.append(f"Сегодня ({today_str}):")
        for t in today_tasks:
            time_part = t.event_time if (not t.all_day and t.event_time) else "весь день"
            lines.append(f"  • id={t.id} [{time_part}] [{t.status}] {t.text}")
    else:
        lines.append(f"Сегодня ({today_str}): задач нет.")

    # Показываем активную задачу только если она не из сегодняшнего плана
    today_ids = {t.id for t in today_tasks}
    if active is not None and active.id not in today_ids:
        time_part = active.event_time if (not active.all_day and active.event_time) else "весь день"
        lines.append(
            f"Последняя активная задача: id={active.id} "
            f"[{active.suggested_date}] [{time_part}] {active.text}"
        )

    return "\n".join(lines)


def build_messages(session: "Session", user_id: int, user_text: str) -> list[dict]:
    system = build_system_prompt()
    summary = build_day_summary(session, user_id)
    if summary:
        system = f"{system}\n\n{summary}"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]
