from app.domain.task_actions import VALID_CATEGORIES


# Параметры для одиночной идентификации задачи (move, snooze, set_category).
_TASK_REF_PROPS = {
    "task_id": {
        "type": "integer",
        "description": (
            "ID задачи. Используй если ID известен из предыдущего ответа инструмента."
        ),
    },
    "task_text": {
        "type": "string",
        "description": (
            "Текст или часть описания задачи для поиска по имени. "
            "Используй если ID неизвестен. Например: 'встреча', 'оплатить счёт'."
        ),
    },
}

# Параметры для операций, поддерживающих батч (complete, delete).
_TASK_BATCH_PROPS = {
    **_TASK_REF_PROPS,
    "task_ids": {
        "type": "array",
        "items": {"type": "integer"},
        "description": (
            "Список ID задач для массовой операции. "
            "Используй когда нужно обработать несколько задач сразу: "
            "'удали обе', 'выполни задачи 1 и 2', 'удали id=5 и id=7'. "
            "Например: task_ids=[5, 7]."
        ),
    },
}


TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": (
                "Создать новую задачу или напоминание на БУДУЩЕЕ. "
                "Используй ТОЛЬКО если пользователь планирует что-то сделать, "
                "хочет напоминание или ставит дело в расписание. "
                "НЕ используй если пользователь сообщает о том, что уже сделал, "
                "съел, выпил, измерил или зафиксировал — для этого есть append_obsidian_log. "
                "Признаки будущей задачи: 'сделать', 'напомни', 'поставь', 'запланируй', "
                "'нужно', 'хочу сделать', 'завтра', 'в пятницу' и т.п. "
                "Если дата не указана и пользователь не намекает на конкретный срок — "
                "передай date=null: задача попадёт в бэклог и будет предложена в свободное время."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Текст задачи без слов даты и времени.",
                    },
                    "date": {
                        "type": ["string", "null"],
                        "description": (
                            "Дата в формате YYYY-MM-DD. "
                            "null если дата не указана — задача попадёт в бэклог."
                        ),
                    },
                    "time": {
                        "type": ["string", "null"],
                        "description": "Время в формате HH:MM или null если время не указано.",
                    },
                    "urgency": {
                        "type": ["integer", "null"],
                        "description": (
                            "Срочность 1–5: насколько скоро нужно сделать. "
                            "5 = сегодня/завтра критично; 1 = когда-нибудь. "
                            "null если непонятно."
                        ),
                    },
                    "importance": {
                        "type": ["integer", "null"],
                        "description": (
                            "Важность 1–5: насколько влияет на цели. "
                            "5 = критически важно; 1 = мелочь. "
                            "null если непонятно."
                        ),
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_priority",
            "description": (
                "Установить приоритет существующей задачи. "
                "Используй когда пользователь говорит 'высокий приоритет', "
                "'это важно', 'срочно', 'не срочно' для конкретной задачи."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": ["integer", "null"]},
                    "task_text": {"type": ["string", "null"], "description": "Текст или ключевые слова задачи если task_id неизвестен."},
                    "urgency":    {"type": "integer", "description": "Срочность 1–5."},
                    "importance": {"type": "integer", "description": "Важность 1–5."},
                },
                "required": ["urgency", "importance"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": (
                "Отметить одну или несколько задач как выполненные. Используй в двух случаях:\n"
                "1. Пользователь явно говорит 'выполнил', 'сделал', 'закончил', 'готово' + "
                "называет задачу.\n"
                "2. Пользователь пишет в прошедшем времени ('я купил хлеб', 'позвонил врачу') "
                "И в списке активных задач есть семантически совпадающая.\n"
                "БАТЧ: если нужно выполнить несколько задач ('обе', 'все', 'задачи 1 и 2') — "
                "передай task_ids=[id1, id2, ...]. "
                "Одиночная задача: task_id или task_text."
            ),
            "parameters": {
                "type": "object",
                "properties": _TASK_BATCH_PROPS,
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": (
                "Удалить одну или несколько задач. Используй когда пользователь хочет удалить, "
                "убрать или отменить задачу. Не используй complete_task для удаления.\n"
                "БАТЧ: если нужно удалить несколько задач ('обе', 'удали 1 и 2', 'удали все') — "
                "передай task_ids=[id1, id2, ...]. "
                "Одиночная задача: task_id или task_text."
            ),
            "parameters": {
                "type": "object",
                "properties": _TASK_BATCH_PROPS,
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_task",
            "description": (
                "Изменить дату и/или время существующей задачи. "
                "Вызывай в трёх случаях:\n"
                "1. Пользователь явно просит перенести: 'перенеси на пятницу', 'поставь время 15:00'.\n"
                "2. Пользователь использует местоимения ('ей', 'этой задаче', 'её', 'тут') "
                "применительно к только что упомянутой или созданной задаче.\n"
                "3. Пользователь уточняет параметры задачи сразу после её создания: "
                "'и поставь ей время с 15 до 18', 'с 9 до 10'.\n"
                "ВАЖНО: если меняется только время — new_date можно не передавать (возьмётся текущая дата задачи). "
                "Если меняется только дата — new_time можно не передавать. "
                "Диапазон времени: new_time='HH:MM-HH:MM'. "
                "НИКОГДА не вызывай create_task или delete_task для изменения задачи. "
                "Передай task_id если известен, иначе task_text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_TASK_REF_PROPS,
                    "new_date": {
                        "type": ["string", "null"],
                        "description": (
                            "Новая дата в формате YYYY-MM-DD. "
                            "Необязателен если меняется только время."
                        ),
                    },
                    "new_time": {
                        "type": ["string", "null"],
                        "description": (
                            "Новое время: 'HH:MM' или диапазон 'HH:MM-HH:MM'. "
                            "null если задача на весь день."
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snooze_task",
            "description": (
                "Отложить задачу до указанной даты. Сбрасывает счётчик напоминаний. "
                "Используй когда пользователь говорит 'напомни позже', 'отложи до ...'. "
                "Передай task_id если известен, иначе task_text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_TASK_REF_PROPS,
                    "until_date": {
                        "type": "string",
                        "description": "Дата отсрочки в формате YYYY-MM-DD.",
                    },
                    "until_time": {
                        "type": ["string", "null"],
                        "description": "Время напоминания в формате HH:MM или null.",
                    },
                },
                "required": ["until_date"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_category",
            "description": (
                "Установить категорию задачи. Используй когда пользователь хочет "
                "классифицировать или организовать задачу. "
                "Передай task_id если известен, иначе task_text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_TASK_REF_PROPS,
                    "category": {
                        "type": "string",
                        "description": "Категория задачи.",
                        "enum": sorted(VALID_CATEGORIES),
                    },
                },
                "required": ["category"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_task_title",
            "description": (
                "Изменить название существующей задачи. Используй когда пользователь хочет "
                "переименовать, исправить опечатку или изменить формулировку задачи. "
                "Передай task_id если известен, иначе task_text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_TASK_REF_PROPS,
                    "new_title": {
                        "type": "string",
                        "description": "Новое название задачи (без дат и времени).",
                    },
                },
                "required": ["new_title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_calendar_events",
            "description": (
                "Прочитать события из Google Calendar на указанную дату или период. "
                "Используй когда пользователь спрашивает о встречах, событиях, расписании: "
                "'что у меня сегодня в календаре', 'есть ли встречи завтра', "
                "'когда следующая встреча', 'свободен ли я в пятницу'. "
                "Возвращает все события включая созданные ботом."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": ["string", "null"],
                        "description": "Дата в формате YYYY-MM-DD. null или отсутствует — сегодня.",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Количество дней начиная с date (1-7). По умолчанию 1.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_today_plan",
            "description": (
                "Получить список задач на конкретную дату. Используй когда пользователь "
                "спрашивает о плане дня: 'что сегодня', 'план на завтра', 'задачи на пятницу'. "
                "Если дата не указана — возвращает задачи на сегодня."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": ["string", "null"],
                        "description": (
                            "Дата в формате YYYY-MM-DD. "
                            "Если не указана или null — возвращает задачи на сегодня."
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_fact_to_obsidian",
            "description": (
                "Сохранить постоянный факт о пользователе в долгосрочную память (Obsidian). "
                "Используй когда пользователь говорит 'запомни', 'запомни что', "
                "'сохрани в памяти', сообщает личную информацию, предпочтения, "
                "постоянный контекст о себе или важный дедлайн. "
                "НЕ используй для разовых лог-записей — для них есть append_obsidian_log."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": (
                            "Факт для записи в память. Конкретная, ёмкая формулировка. "
                            "Например: 'не ест молочное', 'дедлайн проекта X — 15 мая 2026'."
                        ),
                    },
                },
                "required": ["fact"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_obsidian_protocol",
            "description": (
                "Прочитать протокол или инструкцию из Obsidian Vault по указанной сфере жизни. "
                "Используй когда нужно узнать правила, контекст или установки по сфере."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sphere": {
                        "type": "string",
                        "description": (
                            "Название сферы — имя файла без расширения .md. "
                            "Например: health, work, finance, sport."
                        ),
                    },
                },
                "required": ["sphere"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_obsidian_log",
            "description": (
                "Записать в дневник/лог Obsidian факт, который УЖЕ произошёл или происходит "
                "ПРЯМО СЕЙЧАС. Это инструмент для журналирования, трекинга привычек и фиксации "
                "свершившихся событий — НЕ для планирования будущего. "
                "Используй когда пользователь сообщает: что съел/выпил, показатели (вес, давление, "
                "шаги), настроение, симптомы, уже выполненное действие, наблюдение. "
                "Признаки лог-записи: глаголы прошедшего/настоящего времени — 'съел', 'выпил', "
                "'сделал', 'замерил', 'почувствовал', 'запиши', 'отметь', 'зафиксируй', "
                "а также конструкции 'запиши в [сферу]: ...'. "
                "НЕ используй create_task для подобных сообщений."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sphere": {
                        "type": "string",
                        "description": (
                            "Название сферы — имя файла без расширения .md. "
                            "Например: health, work, finance, sport."
                        ),
                    },
                    "entry": {
                        "type": "string",
                        "description": "Текст записи для добавления в лог.",
                    },
                },
                "required": ["sphere", "entry"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_logs",
            "description": (
                "Статистический анализ дневника по сфере: среднее, лучшее/худшее, streak, частота. "
                "Используй когда пользователь спрашивает о ТРЕНДАХ или СТАТИСТИКЕ: "
                "'сколько я в среднем сплю', 'как часто я тренируюсь', 'средний сон за месяц', "
                "'сколько раз был в зале', 'какой у меня streak'. "
                "Сервер считает агрегаты сам — не нужно делать это в тексте ответа. "
                "Sphere обязателен — нельзя анализировать все сферы сразу."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sphere": {
                        "type": "string",
                        "description": (
                            "Сфера: sleep, nutrition, training, german, health, ideas, ivan_context."
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "description": "Глубина выборки в днях. По умолчанию 14.",
                    },
                },
                "required": ["sphere"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_logs",
            "description": (
                "Вернуть СЫРЫЕ записи из дневника за период. "
                "Используй когда нужны конкретные записи: 'что я ел вчера', "
                "'покажи тренировки за неделю', 'что я записывал про сон'. "
                "Для статистики (среднее, streak, частота) — используй analyze_logs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sphere": {
                        "type": ["string", "null"],
                        "description": (
                            "Сфера: sleep, nutrition, training, german, health, ideas, ivan_context. "
                            "null — все сферы."
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "description": "Глубина выборки в днях. По умолчанию 7.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_bot_log",
            "description": (
                "Прочитать последние записи из лог-файла в папке _bot/ Obsidian Vault. "
                "Используй когда пользователь просит показать что записалось, "
                "прочитать лог, проверить запись, посмотреть данные. "
                "НИКОГДА не вызывай create_task вместо этого инструмента. "
                "filename — имя файла с расширением .md (например, 'sleep.md', 'nutrition.md', 'health.md')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": (
                            "Имя файла в _bot/ с расширением .md. "
                            "Примеры: 'sleep.md', 'nutrition.md', 'training.md', 'health.md', 'german.md'."
                        ),
                    },
                },
                "required": ["filename"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_energy",
            "description": (
                "Записать текущий уровень энергии/состояния пользователя (1-10). "
                "Используй когда пользователь говорит о своём состоянии: "
                "'я уставший', 'чувствую себя отлично', 'устал от встреч', 'энергия на нуле', "
                "'в ударе сегодня', 'разбитый', 'бодрый'. "
                "Переводи описание в число: уставший → 2-3, средне → 5-6, отлично → 8-9. "
                "НЕ используй для физических симптомов болезни — это в append_obsidian_log(sphere='health')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "integer",
                        "description": "Уровень энергии от 1 (совсем нет сил) до 10 (максимальная).",
                    },
                    "notes": {
                        "type": ["string", "null"],
                        "description": "Краткая заметка о состоянии. Например: 'устал от встреч', 'хорошо поработал'.",
                    },
                },
                "required": ["level"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory_from_obsidian",
            "description": (
                "Прочитать всю долгосрочную память пользователя из Obsidian. "
                "Используй когда нужно вспомнить ранее сохранённые факты, "
                "предпочтения или личный контекст пользователя."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_recurring_task",
            "description": (
                "Создать повторяющееся событие/напоминание на период. "
                "Используй когда пользователь говорит 'каждый день', 'каждый понедельник', "
                "'ежедневно', 'по рабочим дням', 'каждую неделю по X', 'до конца месяца'. "
                "Задача будет автоматически создаваться каждое утро в 07:00. "
                "НЕ создавай обычную задачу через create_task для повторяющихся событий."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Название задачи (без слов 'каждый день' и т.п.).",
                    },
                    "recurrence": {
                        "type": "string",
                        "description": (
                            "Паттерн повторения: "
                            "'daily' — каждый день, "
                            "'weekdays' — по рабочим дням (Пн-Пт), "
                            "'weekly:0' — каждый понедельник, "
                            "'weekly:1' — каждый вторник, ..., "
                            "'weekly:6' — каждое воскресенье."
                        ),
                    },
                    "time": {
                        "type": ["string", "null"],
                        "description": "Время в формате HH:MM или HH:MM-HH:MM. Null если без времени.",
                    },
                    "end_date": {
                        "type": ["string", "null"],
                        "description": (
                            "Дата окончания YYYY-MM-DD. Null = бессрочно. "
                            "Примеры: 'до конца мая' → '2026-05-31', 'до экзамена 20 июня' → '2026-06-20'."
                        ),
                    },
                },
                "required": ["text", "recurrence"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recurring_tasks",
            "description": (
                "Показать список активных повторяющихся задач пользователя. "
                "Используй когда спрашивают 'какие у меня повторяющиеся задачи', "
                "'/recurring', 'покажи повторяющиеся события'."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]
