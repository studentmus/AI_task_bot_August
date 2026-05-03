from app.domain.task_actions import VALID_CATEGORIES
from app.domain.memory_service import MEMORY_TYPES


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
                "что сделал задачу, закончил, выполнил. "
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
            "name": "propose_memory_save",
            "description": (
                "Предложить пользователю сохранить что-то в долгосрочную память. "
                "Используй когда пользователь сообщает личную информацию, предпочтения "
                "или контекст, который поможет в будущем. "
                "Пользователь сам подтвердит или отклонит сохранение."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Что именно запомнить. Конкретная фраза, не пересказ.",
                    },
                    "memory_type": {
                        "type": "string",
                        "description": "Тип записи.",
                        "enum": sorted(MEMORY_TYPES.keys()),
                    },
                },
                "required": ["content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "Поиск в подтверждённой памяти пользователя. Используй когда нужно "
                "вспомнить что-то ранее сохранённое. Пустой query вернёт все записи."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Поисковый запрос. Пустая строка — вернуть всё.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]
