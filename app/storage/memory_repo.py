from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.storage.db import MemoryItem


class MemoryRepo:
    def __init__(self, session: Session) -> None:
        self._s = session

    def propose_memory(
        self,
        user_id: int,
        content: str,
        memory_type: str,
    ) -> int:
        item = MemoryItem(
            user_id=user_id,
            content=content,
            memory_type=memory_type,
            status="proposed",
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._s.add(item)
        self._s.flush()
        return int(item.id)

    def get(self, memory_id: int) -> Optional[MemoryItem]:
        return self._s.get(MemoryItem, memory_id)

    def confirm_memory(self, memory_id: int) -> bool:
        item = self._s.get(MemoryItem, memory_id)
        if item is None:
            return False
        item.status = "confirmed"
        item.confirmed_at = datetime.now().isoformat(timespec="seconds")
        return True

    def reject_memory(self, memory_id: int) -> bool:
        item = self._s.get(MemoryItem, memory_id)
        if item is None:
            return False
        item.status = "rejected"
        return True

    def search_confirmed_memories(
        self,
        user_id: int,
        query: str = "",
        limit: int = 20,
    ) -> list[MemoryItem]:
        q = self._s.query(MemoryItem).filter(
            MemoryItem.user_id == user_id,
            MemoryItem.status == "confirmed",
        )
        if query:
            q = q.filter(MemoryItem.content.ilike(f"%{query}%"))
        return q.order_by(MemoryItem.id.desc()).limit(limit).all()
