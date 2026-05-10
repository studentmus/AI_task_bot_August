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
        task_id = self._repo.insert_pending(
            text=parsed.clean_text,
            date=parsed.date,
            event_time=parsed.time,
            all_day=parsed.all_day,
            user_id=user_id,
        )
        self._session.commit()
        logger.info("Task created id=%s parser=%s", task_id, parsed.parser)

        task = self._repo.get(task_id)
        if task is not None:
            try:
                from app.domain.google_calendar import create_event
                event_id = create_event(task)
                if event_id:
                    self._repo.mark_synced(task_id, event_id)
                    self._session.commit()
                    logger.info("Task synced to Google Calendar id=%s event_id=%s", task_id, event_id)
            except Exception:
                logger.exception("Google Calendar sync failed for task id=%s", task_id)

        return task_id

    def confirm_and_sync(self, task_id: int) -> Optional[str]:
        """Подтверждает задачу в SQLite. Calendar sync уже выполнен при create_task."""
        if not self._repo.confirm(task_id):
            raise ValueError(f"Task {task_id} not found")
        self._session.commit()
        logger.info("Task confirmed id=%s", task_id)
        return None
