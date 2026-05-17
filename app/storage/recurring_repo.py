from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.storage.db import RecurringTask


_VALID_RECURRENCES = {"daily", "weekdays"}


def _is_valid_recurrence(r: str) -> bool:
    if r in _VALID_RECURRENCES:
        return True
    if r.startswith("weekly:"):
        try:
            n = int(r.split(":")[1])
            return 0 <= n <= 6
        except (ValueError, IndexError):
            return False
    return False


class RecurringRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def create(
        self,
        user_id: int,
        text: str,
        recurrence: str,
        event_time: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> RecurringTask:
        if not _is_valid_recurrence(recurrence):
            raise ValueError(
                f"Неверный формат повторения: {recurrence!r}. "
                "Допустимые: 'daily', 'weekdays', 'weekly:0'...'weekly:6'"
            )
        rt = RecurringTask(
            telegram_user_id=user_id,
            text=text,
            event_time=event_time,
            recurrence=recurrence,
            start_date=start_date or date.today().isoformat(),
            end_date=end_date,
            active=True,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._s.add(rt)
        self._s.flush()
        return rt

    def list_active(self, user_id: int) -> list[RecurringTask]:
        return (
            self._s.query(RecurringTask)
            .filter(
                RecurringTask.telegram_user_id == user_id,
                RecurringTask.active.is_(True),
            )
            .order_by(RecurringTask.id.asc())
            .all()
        )

    def get_due_today(self, user_id: int, today: str, weekday: int) -> list[RecurringTask]:
        """Active recurring tasks that should fire today."""
        candidates = (
            self._s.query(RecurringTask)
            .filter(
                RecurringTask.telegram_user_id == user_id,
                RecurringTask.active.is_(True),
                RecurringTask.start_date <= today,
            )
            .all()
        )
        result = []
        for rt in candidates:
            if rt.end_date and rt.end_date < today:
                continue
            if rt.recurrence == "daily":
                result.append(rt)
            elif rt.recurrence == "weekdays" and weekday < 5:
                result.append(rt)
            elif rt.recurrence.startswith("weekly:"):
                try:
                    target = int(rt.recurrence.split(":")[1])
                    if weekday == target:
                        result.append(rt)
                except (ValueError, IndexError):
                    pass
        return result

    def get_all_due_today(self, today: str, weekday: int) -> list[RecurringTask]:
        """Due recurring tasks for ALL users (used by scheduler)."""
        candidates = (
            self._s.query(RecurringTask)
            .filter(
                RecurringTask.active.is_(True),
                RecurringTask.start_date <= today,
            )
            .all()
        )
        result = []
        for rt in candidates:
            if rt.end_date and rt.end_date < today:
                continue
            if rt.recurrence == "daily":
                result.append(rt)
            elif rt.recurrence == "weekdays" and weekday < 5:
                result.append(rt)
            elif rt.recurrence.startswith("weekly:"):
                try:
                    target = int(rt.recurrence.split(":")[1])
                    if weekday == target:
                        result.append(rt)
                except (ValueError, IndexError):
                    pass
        return result

    def deactivate(self, rt_id: int, user_id: int) -> bool:
        rt = self._s.get(RecurringTask, rt_id)
        if rt is None or rt.telegram_user_id != user_id:
            return False
        rt.active = False
        return True
