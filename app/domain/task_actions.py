import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.storage.db import Task
from app.storage.task_repo import TaskRepo


logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"inbox", "work", "personal", "health", "finance", "study", "other"}


class TaskActions:
    """Бизнес-операции над задачами с commit и человекочитаемым результатом.

    Каждый метод возвращает строку — описание что произошло.
    Это готовит интерфейс для future DeepSeek tool-use.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._repo = TaskRepo(session)

    def _get_or_raise(self, task_id: int) -> Task:
        task = self._repo.get(task_id)
        if task is None:
            raise ValueError(f"Задача {task_id} не найдена.")
        return task

    # ------------------------------------------------------------------
    # Статус
    # ------------------------------------------------------------------

    def complete_task(self, task_id: int) -> str:
        task = self._get_or_raise(task_id)
        if task.status == "done":
            raise ValueError(f"Задача «{task.text}» уже выполнена.")
        name = task.text
        uid = task.radicale_uid
        self._repo.complete_task(task_id)
        self._session.commit()
        if uid:
            try:
                from app.calendar.radicale_sync import delete_task_calendar
                delete_task_calendar(uid)
            except ImportError:
                pass
        logger.info("Task completed id=%s", task_id)
        return f"✅ «{name}» — выполнено."

    def delete_task(self, task_id: int) -> str:
        task = self._get_or_raise(task_id)
        name = task.text
        uid = task.radicale_uid
        task.status = "cancelled"
        self._session.commit()
        if uid:
            try:
                from app.calendar.radicale_sync import delete_task_calendar
                delete_task_calendar(uid)
            except ImportError:
                pass
        logger.info("Task deleted id=%s", task_id)
        return f"🗑 «{name}» — удалено."

    # ------------------------------------------------------------------
    # Перенос / откладывание
    # ------------------------------------------------------------------

    def move_task(
        self,
        task_id: int,
        new_date: str,
        new_time: Optional[str] = None,
        all_day: bool = True,
    ) -> str:
        task = self._get_or_raise(task_id)
        self._repo.move_task(task_id, new_date, new_time, all_day)
        self._session.commit()
        time_part = f" в {new_time}" if new_time else ""
        logger.info("Task moved id=%s → %s%s", task_id, new_date, time_part)
        return f"📅 «{task.text}» перенесено на {new_date}{time_part}."

    def snooze_task(
        self,
        task_id: int,
        until_date: str,
        until_time: Optional[str] = None,
    ) -> str:
        task = self._get_or_raise(task_id)
        self._repo.snooze_task(task_id, until_date, until_time)
        self._session.commit()
        time_part = f" в {until_time}" if until_time else ""
        logger.info("Task snoozed id=%s → %s%s", task_id, until_date, time_part)
        return f"⏰ «{task.text}» отложено до {until_date}{time_part}. Счётчик пингов сброшен."

    # ------------------------------------------------------------------
    # Метаданные
    # ------------------------------------------------------------------

    def set_category(self, task_id: int, category: str) -> str:
        if category not in VALID_CATEGORIES:
            raise ValueError(
                f"Неизвестная категория «{category}». "
                f"Допустимые: {', '.join(sorted(VALID_CATEGORIES))}."
            )
        task = self._get_or_raise(task_id)
        self._repo.set_category(task_id, category)
        self._session.commit()
        logger.info("Task category set id=%s → %s", task_id, category)
        return f"🏷 «{task.text}» → категория «{category}»."

    # ------------------------------------------------------------------
    # Планирование дня
    # ------------------------------------------------------------------

    def get_today_plan(self, user_id: int, today: Optional[str] = None) -> list[Task]:
        return self._repo.get_today_plan(user_id, today)

    def get_active_task(self, user_id: int) -> Optional[Task]:
        return self._repo.get_active_task(user_id)
