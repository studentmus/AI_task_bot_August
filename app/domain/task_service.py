import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.parsing.task_engine import ParseResult
from app.storage.task_repo import TaskRepo


logger = logging.getLogger(__name__)


class TaskService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repo = TaskRepo(session)

    def create_task(self, parsed: ParseResult, user_id: int) -> int:
        """Сохраняет задачу в SQLite со статусом pending.
        Google Calendar sync НЕ выполняется здесь — задача ещё не подтверждена
        и время может быть уточнено пользователем. Sync происходит в confirm_and_sync."""
        task_id = self._repo.insert_pending(
            text=parsed.clean_text,
            date=parsed.date,
            event_time=parsed.time,
            all_day=parsed.all_day,
            user_id=user_id,
        )
        self._session.commit()
        logger.info("Task created (pending, not synced) id=%s parser=%s", task_id, parsed.parser)
        return task_id

    def cleanup_stale_pending(self, older_than_hours: int = 1) -> int:
        """Отменяет pending-задачи без google_event_id, созданные N+ часов назад.
        Используй через /cleanup для разовой очистки мусора."""
        from datetime import datetime, timedelta
        from app.storage.db import Task
        cutoff = (datetime.now() - timedelta(hours=older_than_hours)).isoformat(timespec="seconds")
        tasks = (
            self._session.query(Task)
            .filter(
                Task.status == "pending",
                Task.google_event_id.is_(None),
                Task.created_at < cutoff,
            )
            .all()
        )
        count = len(tasks)
        for task in tasks:
            task.status = "cancelled"
        self._session.commit()
        logger.info("Stale cleanup: %d tasks cancelled (older_than=%dh)", count, older_than_hours)
        return count

    def cleanup_query_phantoms(self, user_id: int) -> int:
        """Отменяет задачи чей текст выглядит как запрос (создались из view-фраз)."""
        from app.storage.db import Task
        _QUERY_STARTS = ("что ", "покажи ", "план ", "что у ", "есть ли ", "какие ", "что на ")
        tasks = (
            self._session.query(Task)
            .filter(
                Task.telegram_user_id == user_id,
                Task.status.in_(["pending", "confirmed"]),
            )
            .all()
        )
        count = 0
        for task in tasks:
            if any(task.text.lower().startswith(p) for p in _QUERY_STARTS):
                task.status = "cancelled"
                count += 1
        if count:
            self._session.commit()
        return count

    def confirm_and_sync(self, task_id: int) -> Optional[str]:
        """Подтверждает задачу и синхронизирует с Google Calendar.
        Sync выполняется здесь, а не при create_task, чтобы дата/время были финальными."""
        if not self._repo.confirm(task_id):
            raise ValueError(f"Task {task_id} not found")
        self._session.commit()
        logger.info("Task confirmed id=%s", task_id)

        task = self._repo.get(task_id)
        if task is None:
            return None

        if task.google_event_id:
            # Уже синхронизировано ранее (например через LLM-путь)
            logger.info("Task id=%s already has google_event_id, skipping create", task_id)
            return task.google_event_id

        try:
            from app.domain.google_calendar import create_event
            event_id = create_event(task)
            if event_id:
                self._repo.mark_synced(task_id, event_id)
                self._session.commit()
                logger.info("Task synced to Google Calendar on confirm id=%s event_id=%s", task_id, event_id)
                return event_id
        except Exception:
            logger.exception("Google Calendar sync failed on confirm for task id=%s", task_id)

        return None
