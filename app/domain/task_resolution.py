from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.storage.db import Task
from app.storage.task_repo import TaskRepo


@dataclass
class ResolveResult:
    status: str  # "found" | "ambiguous" | "not_found"
    task: Optional[Task] = None
    candidates: list[Task] = field(default_factory=list)


def resolve_task_reference(
    session: Session,
    user_id: int,
    text: str,
) -> ResolveResult:
    """Резолвит текстовое описание задачи в конкретный Task.

    Порядок поиска:
    1. Точное совпадение текста (case-insensitive) → found
    2. Частичное совпадение (ILIKE %text%) →
       - 1 результат → found
       - >1 результатов → ambiguous
       - 0 результатов → not_found
    """
    text = text.strip()
    if not text:
        return ResolveResult(status="not_found")

    repo = TaskRepo(session)

    # Шаг 1: точное совпадение
    exact = (
        session.query(Task)
        .filter(
            Task.telegram_user_id == user_id,
            Task.status.notin_(["done", "cancelled"]),
            func.lower(Task.text) == text.lower(),
        )
        .all()
    )
    if len(exact) == 1:
        return ResolveResult(status="found", task=exact[0])

    # Шаг 2: частичное совпадение среди активных задач
    candidates = repo.find_recent_tasks_by_text(user_id, text)

    if len(candidates) == 1:
        return ResolveResult(status="found", task=candidates[0])
    if len(candidates) > 1:
        return ResolveResult(status="ambiguous", candidates=candidates)

    # Шаг 3: фолбэк — ищем среди выполненных/отменённых (для delete_task)
    done_exact = (
        session.query(Task)
        .filter(
            Task.telegram_user_id == user_id,
            Task.status.in_(["done", "cancelled"]),
            func.lower(Task.text) == text.lower(),
        )
        .all()
    )
    if len(done_exact) == 1:
        return ResolveResult(status="found", task=done_exact[0])

    done_partial = (
        session.query(Task)
        .filter(
            Task.telegram_user_id == user_id,
            Task.status.in_(["done", "cancelled"]),
            Task.text.ilike(f"%{text}%"),
        )
        .order_by(Task.id.desc())
        .limit(5)
        .all()
    )
    if not done_partial:
        return ResolveResult(status="not_found")
    if len(done_partial) == 1:
        return ResolveResult(status="found", task=done_partial[0])
    return ResolveResult(status="ambiguous", candidates=done_partial)


def format_candidates(candidates: list[Task]) -> str:
    """Форматирует список кандидатов для отображения пользователю / LLM."""
    lines = []
    for t in candidates:
        time_part = t.event_time if (not t.all_day and t.event_time) else "весь день"
        lines.append(f"  id={t.id} | {t.suggested_date} | {time_part} | {t.text}")
    return "\n".join(lines)
