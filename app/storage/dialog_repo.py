from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.storage.db import DialogMessage


# Артефакты ошибок и технический мусор, который не должен попадать в историю.
# Бот не должен «помнить» свои баги и повторять их.
_HISTORY_NOISE: tuple[str, ...] = (
    "❌ Задача удалена",
    "ValueError",
    "⚠️ Задача не найдена",
    "⚠️ Не смог",
    "Ошибка при выполнении",
)


class DialogRepo:
    # Окно чтения: последние N сообщений подаются в LLM как история
    KEEP_MESSAGES = 20  # 10 ходов (user+assistant)
    # Время жизни записей: сообщения старше суток удаляются при очередном обращении
    TTL_HOURS = 24

    def __init__(self, session: Session) -> None:
        self._s = session

    def append(self, user_id: int, role: str, content: str) -> None:
        """Добавляет сообщение в историю. Артефакты ошибок пропускаются."""
        if any(noise in content for noise in _HISTORY_NOISE):
            return  # не «отравляем» историю техническим мусором
        self._s.add(DialogMessage(
            user_id=user_id,
            role=role,
            content=content,
            created_at=datetime.now().isoformat(timespec="seconds"),
        ))

    def get_recent(self, user_id: int) -> list[dict]:
        """Возвращает последние KEEP_MESSAGES сообщений в хронологическом порядке."""
        rows = (
            self._s.query(DialogMessage)
            .filter(DialogMessage.user_id == user_id)
            .order_by(DialogMessage.id.desc())
            .limit(self.KEEP_MESSAGES)
            .all()
        )
        return [{"role": r.role, "content": r.content} for r in reversed(rows)]

    def purge_old(self, user_id: int) -> int:
        """Удаляет сообщения пользователя старше TTL_HOURS. Вызывается лениво при каждом чтении."""
        cutoff = (datetime.now() - timedelta(hours=self.TTL_HOURS)).isoformat(timespec="seconds")
        return (
            self._s.query(DialogMessage)
            .filter(
                DialogMessage.user_id == user_id,
                DialogMessage.created_at < cutoff,
            )
            .delete(synchronize_session=False)
        )

    def purge_artifacts(self, user_id: int) -> int:
        """Удаляет из истории записи с артефактами ошибок (для /cleanup)."""
        total = 0
        for noise in _HISTORY_NOISE:
            total += (
                self._s.query(DialogMessage)
                .filter(
                    DialogMessage.user_id == user_id,
                    DialogMessage.content.contains(noise),
                )
                .delete(synchronize_session=False)
            )
        return total
