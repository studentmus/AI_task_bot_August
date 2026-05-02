from datetime import date

from app.config import settings


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
