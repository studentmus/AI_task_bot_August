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
        return task_id

    def confirm_and_sync(self, task_id: int) -> Optional[str]:
        """Подтверждает задачу и синхронизирует с Radicale.

        Подтверждение всегда коммитится.
        Возвращает radicale_uid если sync прошёл, None если модуль недоступен или sync упал.
        """
        if not self._repo.confirm(task_id):
            raise ValueError(f"Task {task_id} not found")
        self._session.commit()
        logger.info("Task confirmed id=%s", task_id)

        try:
            from app.calendar.radicale_sync import sync_task
        except (ImportError, ModuleNotFoundError):
            logger.warning("radicale_sync unavailable, calendar sync skipped for task id=%s", task_id)
            return None

        task = self._repo.get(task_id)
        try:
            uid = sync_task(task)
            self._repo.mark_synced(task_id, uid)
            self._session.commit()
            logger.info("Task synced id=%s uid=%s", task_id, uid)
            return uid
        except Exception:
            logger.exception("Radicale sync failed for task id=%s", task_id)
            return None
