import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.storage.db import MemoryItem
from app.storage.memory_repo import MemoryRepo


logger = logging.getLogger(__name__)

MEMORY_TYPES = {
    "fact": "факт",
    "preference": "предпочтение",
    "context": "контекст",
    "pattern": "паттерн",
}


class MemoryService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repo = MemoryRepo(session)

    def propose(
        self,
        user_id: int,
        content: str,
        memory_type: str = "fact",
    ) -> int:
        memory_id = self._repo.propose_memory(user_id, content, memory_type)
        self._session.commit()
        logger.info("Memory proposed id=%s type=%s user=%s", memory_id, memory_type, user_id)
        return memory_id

    def confirm(self, memory_id: int) -> str:
        item = self._repo.get(memory_id)
        if item is None:
            raise ValueError(f"Запись памяти {memory_id} не найдена.")
        if item.status == "confirmed":
            raise ValueError(f"Запись {memory_id} уже сохранена.")
        content = item.content
        self._repo.confirm_memory(memory_id)
        self._session.commit()
        logger.info("Memory confirmed id=%s", memory_id)
        return f"✅ Запомнено: «{content}»"

    def reject(self, memory_id: int) -> str:
        item = self._repo.get(memory_id)
        if item is None:
            raise ValueError(f"Запись памяти {memory_id} не найдена.")
        if item.status in ("confirmed", "rejected"):
            raise ValueError(f"Запись {memory_id} уже обработана.")
        self._repo.reject_memory(memory_id)
        self._session.commit()
        logger.info("Memory rejected id=%s", memory_id)
        return "❌ Не сохранено."

    def search(
        self,
        user_id: int,
        query: str = "",
        limit: int = 20,
    ) -> list[MemoryItem]:
        return self._repo.search_confirmed_memories(user_id, query, limit)

    def format_for_context(self, user_id: int, limit: int = 10) -> str:
        """Возвращает подтверждённые воспоминания строкой для вставки в промпт."""
        items = self._repo.search_confirmed_memories(user_id, limit=limit)
        if not items:
            return ""
        lines = ["Известно о пользователе:"]
        for item in reversed(items):  # хронологический порядок
            type_label = MEMORY_TYPES.get(item.memory_type, item.memory_type)
            lines.append(f"  [{type_label}] {item.content}")
        return "\n".join(lines)
