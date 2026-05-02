from app.domain.task_actions import VALID_CATEGORIES


TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": (
                "Создать новую задачу. Используй когда пользователь хочет записать дело, "
                "событие или напоминание. Дату и время извлеки из текста."
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
                "Отметить задачу как выполненную. Используй когда пользователь говорит "
                "что сделал задачу, закончил, выполнил."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "ID задачи.",
                    },
                },
                "required": ["task_id"],
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
                "хочет изменить дату или время существующей задачи."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "ID задачи.",
                    },
                    "new_date": {
                        "type": "string",
                        "description": "Новая дата в формате YYYY-MM-DD.",
                    },
                    "new_time": {
                        "type": ["string", "null"],
                        "description": "Новое время в формате HH:MM или null.",
                    },
                },
                "required": ["task_id", "new_date"],
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
                "Используй когда пользователь говорит 'напомни позже', 'отложи до ...'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "ID задачи.",
                    },
                    "until_date": {
                        "type": "string",
                        "description": "Дата отсрочки в формате YYYY-MM-DD.",
                    },
                    "until_time": {
                        "type": ["string", "null"],
                        "description": "Время напоминания в формате HH:MM или null.",
                    },
                },
                "required": ["task_id", "until_date"],
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
                "классифицировать или организовать задачу."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "ID задачи.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Категория задачи.",
                        "enum": sorted(VALID_CATEGORIES),
                    },
                },
                "required": ["task_id", "category"],
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
]
