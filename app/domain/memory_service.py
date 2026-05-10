import logging

from sqlalchemy.orm import Session

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

    def format_for_context(self, user_id: int, limit: int = 10) -> str:
        """Возвращает ранее подтверждённые записи из SQLite для вставки в промпт.
        Актуально для данных, созданных до миграции памяти в Obsidian."""
        items = self._repo.search_confirmed_memories(user_id, limit=limit)
        if not items:
            return ""
        lines = ["Известно о пользователе (старая память):"]
        for item in reversed(items):
            type_label = MEMORY_TYPES.get(item.memory_type, item.memory_type)
            lines.append(f"  [{type_label}] {item.content}")
        return "\n".join(lines)
