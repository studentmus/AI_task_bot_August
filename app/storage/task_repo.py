from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.storage.db import Task


class TaskRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def insert_pending(
        self,
        text: str,
        date: str,
        event_time: Optional[str],
        all_day: bool,
        user_id: int,
    ) -> int:
        task = Task(
            text=text,
            category="inbox",
            importance="medium",
            suggested_date=date,
            event_time=event_time,
            all_day=all_day,
            status="pending",
            created_at=datetime.now().isoformat(timespec="seconds"),
            telegram_user_id=user_id,
        )
        self._s.add(task)
        self._s.flush()
        return int(task.id)

    def get(self, task_id: int) -> Optional[Task]:
        return self._s.get(Task, task_id)

    def confirm(self, task_id: int) -> bool:
        task = self._s.get(Task, task_id)
        if task is None:
            return False
        task.status = "confirmed"
        task.confirmed_at = datetime.now().isoformat(timespec="seconds")
        return True

    def delete(self, task_id: int) -> bool:
        task = self._s.get(Task, task_id)
        if task is None:
            return False
        self._s.delete(task)
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
                (Task.radicale_uid == None) | (Task.radicale_uid == ""),
            )
            .order_by(Task.id.asc())
            .all()
        )

    def mark_synced(self, task_id: int, uid: str) -> None:
        task = self._s.get(Task, task_id)
        if task is not None:
            task.radicale_uid = uid
