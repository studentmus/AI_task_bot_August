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

Верни JSON строго в виде:
{{"tasks": [{{"date": "YYYY-MM-DD", "time": "HH:MM или null", "all_day": true/false, "clean_text": "текст без дат и времени"}}]}}

Если пользователь перечисляет несколько дел в одном сообщении (например, "с 10 до 15 работа, а в 17 день рождения") — разделяй их на отдельные элементы массива tasks. Если дело одно — массив из одного элемента.

Правила:
- "вечером" = 19:00
- "утром" = 09:00
- "днём" / "днем" = 14:00
- "ночью" = 22:00
- "в 3 часа" без уточнения = 15:00
- "в пятницу" = ближайшая будущая пятница
- "через N дней" считай от сегодняшней даты
- Если пользователь указал диапазон времени (например, "с 10:00 до 15:00"), записывай в поле time через дефис: "10:00-15:00". all_day = false.
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
        "переносить и откладывать дела, смотреть план дня, а также вести личный дневник.\n\n"
        "## Ключевое правило: задача vs. лог\n\n"
        "Перед каждым ответом определи тип намерения:\n\n"
        "БУДУЩЕЕ ДЕЙСТВИЕ → create_task\n"
        "  Пользователь планирует что-то сделать, хочет напоминание, ставит дело.\n"
        "  Маркеры: 'напомни', 'поставь', 'запланируй', 'нужно сделать', 'хочу сделать',\n"
        "  'завтра', 'в пятницу', 'через неделю', будущее/инфинитив ('купить', 'позвонить').\n"
        "  Пример: 'Напомни купить молоко завтра' → create_task\n\n"
        "СВЕРШИВШИЙСЯ ФАКТ / ЛОГ → append_obsidian_log\n"
        "  Пользователь сообщает о том, что уже произошло или происходит прямо сейчас,\n"
        "  хочет зафиксировать данные, вести трекинг привычек или дневник.\n"
        "  Маркеры: 'запиши в [сферу]:', 'съел', 'выпил', 'сделал', 'замерил', 'вес',\n"
        "  'давление', 'шаги', 'настроение', 'сегодня было', 'отметь', 'зафиксируй'.\n"
        "  Пример: 'Запиши в питание: съел протеиновый батончик' → append_obsidian_log\n"
        "  Пример: 'Вес сегодня 78 кг' → append_obsidian_log (sphere=health)\n\n"
        "ВАЖНО: конструкция 'запиши в [сферу]: ...' — это ВСЕГДА лог, никогда не задача.\n"
        "Конструкция 'в [сферу] запиши: ...' — тоже всегда лог (обратный порядок слов).\n\n"
        "## Анти-галлюцинация (абсолютный запрет)\n\n"
        "ЗАПРЕЩЕНО писать 'записал', 'добавлено', 'готово, зафиксировал', 'отметил' или\n"
        "любые синонимы подтверждения выполненного действия, если ты НЕ вызвала tool.\n"
        "Правило: подтверждение = вызов tool. Без вызова — нет подтверждения.\n"
        "Если сфера непонятна или инструмент недоступен — ответь честно:\n"
        "'Не могу записать: укажи сферу (питание, сон, тренировки...)'\n"
        "или задай уточняющий вопрос. Фантазировать о выполненном действии недопустимо.\n\n"
        "Остальные правила:\n"
        "- Используй инструменты для любых действий с задачами.\n"
        "- Отвечай коротко, по-русски, без лишних слов.\n"
        "- Если намерение неясно — уточни одним вопросом.\n"
        "- Не придумывай task_id. Если нужен ID — сначала вызови get_active_task или get_today_plan.\n"
        "- Если append_obsidian_log вернул ошибку 'Протокол не существует': дословно перескажи\n"
        "  пользователю список доступных сфер из ответа инструмента и спроси, хочет ли он\n"
        "  создать новый протокол или выбрать из существующих. Не сообщай об успехе при ошибке."
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


def build_memory_context(session: "Session", user_id: int) -> str:
    """Возвращает подтверждённые воспоминания для вставки в system prompt."""
    from app.domain.memory_service import MemoryService
    return MemoryService(session).format_for_context(user_id)


def build_messages(session: "Session", user_id: int, user_text: str) -> list[dict]:
    parts = [build_system_prompt()]

    memory_ctx = build_memory_context(session, user_id)
    if memory_ctx:
        parts.append(memory_ctx)

    day_summary = build_day_summary(session, user_id)
    if day_summary:
        parts.append(day_summary)

    return [
        {"role": "system", "content": "\n\n".join(parts)},
        {"role": "user", "content": user_text},
    ]
