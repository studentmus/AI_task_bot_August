from app.domain.task_actions import VALID_CATEGORIES


# Общий блок параметров для инструментов, которым нужна идентификация задачи.
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
                "'нужно', 'хочу сделать', 'завтра', 'в пятницу' и т.п."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Текст задачи без слов даты и времени.",
                    },
                    "date": {
                        "type": "string",
                        "description": "Дата в формате YYYY-MM-DD.",
                    },
                    "time": {
                        "type": ["string", "null"],
                        "description": "Время в формате HH:MM или null если время не указано.",
                    },
                },
                "required": ["text", "date"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": (
                "Отметить задачу как выполненную. Используй в двух случаях:\n"
                "1. Пользователь явно говорит 'выполнил', 'сделал', 'закончил', 'готово' + "
                "называет задачу.\n"
                "2. Пользователь пишет в прошедшем времени ('я купил хлеб', 'позвонил врачу', "
                "'сходил в магазин') И в списке активных задач есть семантически совпадающая. "
                "ПРИОРИТЕТ: прошедшее время + совпадение с задачей → ВСЕГДА complete_task, "
                "даже если сообщение внешне похоже на лог-запись. "
                "Передай task_id если известен, иначе task_text с описанием задачи."
            ),
            "parameters": {
                "type": "object",
                "properties": _TASK_REF_PROPS,
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
                "Удалить задачу. Используй когда пользователь хочет удалить, убрать или "
                "отменить задачу. Не используй complete_task для удаления. "
                "Передай task_id если известен, иначе task_text с описанием задачи."
            ),
            "parameters": {
                "type": "object",
                "properties": _TASK_REF_PROPS,
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
                "Перенести задачу на другую дату или время. Используй когда пользователь "
                "хочет изменить дату или время существующей задачи. "
                "Передай task_id если известен, иначе task_text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_TASK_REF_PROPS,
                    "new_date": {
                        "type": "string",
                        "description": "Новая дата в формате YYYY-MM-DD.",
                    },
                    "new_time": {
                        "type": ["string", "null"],
                        "description": "Новое время в формате HH:MM или null.",
                    },
                },
                "required": ["new_date"],
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
            "name": "get_today_plan",
            "description": (
                "Получить список задач на сегодня. Используй когда пользователь спрашивает "
                "о плане дня, что запланировано, что нужно сделать сегодня."
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
            "name": "get_active_task",
            "description": (
                "Получить последнюю активную (незавершённую) задачу пользователя. "
                "Используй когда пользователь говорит 'эта задача', 'текущая задача' "
                "без явного указания ID."
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
]
