import json
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.storage.db import LogEntry


class LogRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def insert(self, user_id: int, sphere: str, raw_text: str, logged_at: str) -> int:
        entry = LogEntry(
            user_id=user_id,
            sphere=sphere,
            raw_text=raw_text,
            logged_at=logged_at,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._s.add(entry)
        self._s.commit()
        return entry.id

    def update_structured_data(self, entry_id: int, data: dict) -> None:
        self._s.query(LogEntry).filter(LogEntry.id == entry_id).update(
            {"structured_data": json.dumps(data, ensure_ascii=False)}
        )
        self._s.commit()

    def get_recent(
        self,
        user_id: int,
        sphere: Optional[str] = None,
        days: int = 7,
        limit: int = 50,
    ) -> list[LogEntry]:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        q = (
            self._s.query(LogEntry)
            .filter(LogEntry.user_id == user_id, LogEntry.logged_at >= cutoff)
        )
        if sphere:
            q = q.filter(LogEntry.sphere == sphere)
        return q.order_by(LogEntry.logged_at.desc()).limit(limit).all()
