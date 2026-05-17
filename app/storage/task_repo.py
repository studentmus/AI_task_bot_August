from datetime import date, datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.storage.db import Task


def _normalize_date(value: str) -> str:
    """Ensure date is ISO YYYY-MM-DD regardless of how it arrived."""
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Cannot normalize date: {value!r}")


class TaskRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------------
    # Создание
    # ------------------------------------------------------------------

    def insert_pending(
        self,
        text: str,
        date: Optional[str],
        event_time: Optional[str],
        all_day: bool,
        user_id: int,
    ) -> int:
        task = Task(
            text=text,
            category="inbox",
            importance="medium",
            suggested_date=_normalize_date(date) if date else None,
            event_time=event_time,
            all_day=all_day,
            status="pending",
            created_at=datetime.now().isoformat(timespec="seconds"),
            telegram_user_id=user_id,
            ping_count=0,
        )
        self._s.add(task)
        self._s.flush()
        return int(task.id)

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------

    def get(self, task_id: int) -> Optional[Task]:
        return self._s.get(Task, task_id)

    def list_recent(self, limit: int = 10) -> list[Task]:
        return (
            self._s.query(Task)
            .order_by(Task.id.desc())
            .limit(limit)
            .all()
        )

    def list_unsynced(self) -> list[Task]:
        return (
            self._s.query(Task)
            .filter(
                Task.status == "confirmed",
                (Task.google_event_id == None) | (Task.google_event_id == ""),
            )
            .order_by(Task.id.asc())
            .all()
        )

    def get_today_plan(self, user_id: int, today: Optional[str] = None) -> list[Task]:
        today_str = today or date.today().isoformat()
        return (
            self._s.query(Task)
            .filter(
                Task.telegram_user_id == user_id,
                Task.suggested_date == today_str,
                Task.status.in_(["pending", "confirmed"]),
            )
            .order_by(Task.event_time.asc().nulls_last(), Task.id.asc())
            .all()
        )

    def get_today_all(self, user_id: int, today: Optional[str] = None) -> list[Task]:
        """Все задачи на сегодня: pending/confirmed/done (cancelled исключены)."""
        today_str = today or date.today().isoformat()
        return (
            self._s.query(Task)
            .filter(
                Task.telegram_user_id == user_id,
                Task.suggested_date == today_str,
                Task.status.in_(["pending", "confirmed", "done"]),
            )
            .order_by(Task.event_time.asc().nulls_last(), Task.id.asc())
            .all()
        )

    def get_today_done(self, user_id: int, today: Optional[str] = None) -> list[Task]:
        """Выполненные задачи на указанную дату (для summary в day view)."""
        today_str = today or date.today().isoformat()
        return (
            self._s.query(Task)
            .filter(
                Task.telegram_user_id == user_id,
                Task.suggested_date == today_str,
                Task.status == "done",
            )
            .order_by(Task.event_time.asc().nulls_last(), Task.id.asc())
            .all()
        )

    def get_active_task(self, user_id: int) -> Optional[Task]:
        """Последняя незавершённая задача пользователя."""
        return (
            self._s.query(Task)
            .filter(
                Task.telegram_user_id == user_id,
                Task.status.notin_(["done", "cancelled"]),
            )
            .order_by(Task.id.desc())
            .first()
        )

    def get_upcoming_tasks(self, user_id: int, from_date: str, limit: int = 20) -> list[Task]:
        """Все невыполненные задачи начиная с from_date (включительно).
        Статусы: всё кроме done/cancelled — т.е. pending, confirmed и любые будущие."""
        return (
            self._s.query(Task)
            .filter(
                Task.telegram_user_id == user_id,
                Task.suggested_date >= from_date,
                Task.status.notin_(["done", "cancelled"]),
            )
            .order_by(Task.suggested_date.asc(), Task.event_time.asc().nulls_last(), Task.id.asc())
            .limit(limit)
            .all()
        )

    def get_backlog_tasks(self, user_id: int, limit: int = 10) -> list[Task]:
        """Задачи без даты (бэклог) — предлагаются в свободное время."""
        return (
            self._s.query(Task)
            .filter(
                Task.telegram_user_id == user_id,
                Task.suggested_date.is_(None),
                Task.status.in_(["pending", "confirmed"]),
            )
            .order_by(Task.id.asc())
            .limit(limit)
            .all()
        )

    def get_recently_done(self, user_id: int, limit: int = 5) -> list[Task]:
        """Последние выполненные задачи — для инъекции в контекст LLM."""
        return (
            self._s.query(Task)
            .filter(
                Task.telegram_user_id == user_id,
                Task.status == "done",
            )
            .order_by(Task.id.desc())
            .limit(limit)
            .all()
        )

    def find_recent_tasks_by_text(
        self, user_id: int, query: str, limit: int = 5
    ) -> list[Task]:
        """Поиск активных задач по подстроке текста.
        Использует func.lower() на обеих сторонах — SQLite ilike не регистронезависим
        для кириллицы без явного lowercasing."""
        q_lower = query.strip().lower()
        return (
            self._s.query(Task)
            .filter(
                Task.telegram_user_id == user_id,
                Task.status.notin_(["done", "cancelled"]),
                func.lower(Task.text).like(f"%{q_lower}%"),
            )
            .order_by(Task.id.desc())
            .limit(limit)
            .all()
        )

    def find_active_or_recent_tasks(
        self, user_id: int, limit: int = 10
    ) -> list[Task]:
        """Все незавершённые задачи пользователя, ближайшие по дате первыми."""
        return (
            self._s.query(Task)
            .filter(
                Task.telegram_user_id == user_id,
                Task.status.notin_(["done", "cancelled"]),
            )
            .order_by(Task.suggested_date.asc().nulls_last(), Task.id.asc())
            .limit(limit)
            .all()
        )

    # ------------------------------------------------------------------
    # Обновление статуса
    # ------------------------------------------------------------------

    def confirm(self, task_id: int) -> bool:
        task = self._s.get(Task, task_id)
        if task is None:
            return False
        task.status = "confirmed"
        task.confirmed_at = datetime.now().isoformat(timespec="seconds")
        return True

    def complete_task(self, task_id: int) -> bool:
        task = self._s.get(Task, task_id)
        if task is None:
            return False
        task.status = "done"
        return True

    def delete(self, task_id: int) -> bool:
        task = self._s.get(Task, task_id)
        if task is None:
            return False
        self._s.delete(task)
        return True

    # ------------------------------------------------------------------
    # Обновление даты / времени
    # ------------------------------------------------------------------

    def update_text(self, task_id: int, new_text: str) -> bool:
        task = self._s.get(Task, task_id)
        if task is None:
            return False
        task.text = new_text
        return True

    def update_date(self, task_id: int, date_str: str) -> bool:
        task = self._s.get(Task, task_id)
        if task is None:
            return False
        task.suggested_date = date_str
        return True

    def update_time(
        self, task_id: int, event_time: Optional[str], all_day: bool
    ) -> bool:
        task = self._s.get(Task, task_id)
        if task is None:
            return False
        task.event_time = event_time
        task.all_day = all_day
        return True

    def update_date_time(
        self,
        task_id: int,
        date_str: str,
        event_time: Optional[str],
        all_day: bool,
    ) -> bool:
        task = self._s.get(Task, task_id)
        if task is None:
            return False
        task.suggested_date = date_str
        task.event_time = event_time
        task.all_day = all_day
        return True

    def move_task(
        self,
        task_id: int,
        new_date: str,
        new_time: Optional[str] = None,
        all_day: bool = True,
    ) -> bool:
        task = self._s.get(Task, task_id)
        if task is None:
            return False
        task.suggested_date = new_date
        task.event_time = new_time
        task.all_day = all_day if new_time is None else False
        return True

    def snooze_task(
        self,
        task_id: int,
        until_date: str,
        until_time: Optional[str] = None,
    ) -> bool:
        task = self._s.get(Task, task_id)
        if task is None:
            return False
        task.suggested_date = until_date
        task.event_time = until_time
        task.all_day = until_time is None
        task.ping_count = 0
        task.last_ping_at = None
        return True

    # ------------------------------------------------------------------
    # Метаданные
    # ------------------------------------------------------------------

    def set_category(self, task_id: int, category: str) -> bool:
        task = self._s.get(Task, task_id)
        if task is None:
            return False
        task.category = category
        return True

    def mark_synced(self, task_id: int, uid: str) -> None:
        task = self._s.get(Task, task_id)
        if task is not None:
            task.google_event_id = uid
