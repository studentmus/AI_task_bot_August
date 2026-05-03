import json
import logging

from sqlalchemy.orm import Session

from app.domain.task_actions import TaskActions
from app.domain.task_resolution import format_candidates, resolve_task_reference
from app.storage.task_repo import TaskRepo


logger = logging.getLogger(__name__)


def execute_tool_call(tool_call: dict, session: Session, user_id: int) -> str:
    """Выполняет один tool call от LLM.

    Принимает tool_call в формате OpenAI/DeepSeek:
        {"id": "...", "function": {"name": "...", "arguments": "{...}"}}

    Возвращает строку-результат для role=tool ответа.
    Для propose_memory_save result содержит JSON с memory_id — message_router
    использует его для вызова send_memory_proposal.
    """
    try:
        name = tool_call["function"]["name"]
        args = json.loads(tool_call["function"]["arguments"])
    except (KeyError, json.JSONDecodeError) as exc:
        logger.warning("Malformed tool_call: %s", exc)
        return _err(f"Неверный формат вызова инструмента: {exc}")

    logger.info("Tool call: %s args=%s", name, args)

    try:
        result = _dispatch(name, args, session, user_id)
        return json.dumps({"ok": True, "result": result}, ensure_ascii=False)
    except ValueError as exc:
        return _err(str(exc))
    except Exception:
        logger.exception("Tool execution failed: %s", name)
        return _err(f"Ошибка при выполнении {name!r}")


def _err(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _resolve_task_id(args: dict, session: Session, user_id: int) -> int:
    task_id = args.get("task_id")
    if task_id is not None:
        return int(task_id)

    query = (args.get("task_text") or "").strip()
    if not query:
        raise ValueError("Укажи task_id или опиши задачу в поле task_text.")

    result = resolve_task_reference(session, user_id, query)

    if result.status == "found":
        return result.task.id
    if result.status == "ambiguous":
        raise ValueError(
            f"Нашёл несколько подходящих задач, уточни какую:\n"
            f"{format_candidates(result.candidates)}"
        )
    raise ValueError(f"Задача по описанию «{query}» не найдена.")


def _dispatch(name: str, args: dict, session: Session, user_id: int) -> str:
    actions = TaskActions(session)

    if name == "create_task":
        time_val = args.get("time") or None
        repo = TaskRepo(session)
        task_id = repo.insert_pending(
            text=args["text"],
            date=args["date"],
            event_time=time_val,
            all_day=time_val is None,
            user_id=user_id,
        )
        session.commit()
        logger.info("Tool create_task → id=%s", task_id)
        return f"Задача создана, id={task_id}: «{args['text']}» на {args['date']}"

    if name == "complete_task":
        return actions.complete_task(_resolve_task_id(args, session, user_id))

    if name == "delete_task":
        return actions.delete_task(_resolve_task_id(args, session, user_id))

    if name == "move_task":
        new_time = args.get("new_time") or None
        return actions.move_task(
            task_id=_resolve_task_id(args, session, user_id),
            new_date=args["new_date"],
            new_time=new_time,
            all_day=new_time is None,
        )

    if name == "snooze_task":
        return actions.snooze_task(
            task_id=_resolve_task_id(args, session, user_id),
            until_date=args["until_date"],
            until_time=args.get("until_time") or None,
        )

    if name == "set_category":
        return actions.set_category(_resolve_task_id(args, session, user_id), args["category"])

    if name == "get_today_plan":
        tasks = actions.get_today_plan(user_id)
        if not tasks:
            return "На сегодня задач нет."
        lines = []
        for t in tasks:
            time_part = t.event_time if (not t.all_day and t.event_time) else "весь день"
            lines.append(f"id={t.id} | {time_part} | [{t.status}] {t.text}")
        return "\n".join(lines)

    if name == "get_active_task":
        task = actions.get_active_task(user_id)
        if task is None:
            return "Активных задач нет."
        time_part = task.event_time if (not task.all_day and task.event_time) else "весь день"
        return f"id={task.id} | {task.suggested_date} | {time_part} | [{task.status}] {task.text}"

    if name == "propose_memory_save":
        from app.domain.memory_service import MemoryService
        content = args["content"].strip()
        memory_type = args.get("memory_type") or "fact"
        svc = MemoryService(session)
        memory_id = svc.propose(user_id, content, memory_type)
        # Возвращаем JSON-строку — message_router извлечёт memory_id для proposal
        return json.dumps({
            "memory_id": memory_id,
            "content": content,
            "memory_type": memory_type,
            "status": "proposed",
        }, ensure_ascii=False)

    if name == "search_memory":
        from app.domain.memory_service import MemoryService
        query = (args.get("query") or "").strip()
        svc = MemoryService(session)
        items = svc.search(user_id, query)
        if not items:
            return "Память пуста." if not query else f"По запросу «{query}» ничего не найдено."
        from app.domain.memory_service import MEMORY_TYPES
        lines = []
        for item in reversed(items):
            type_label = MEMORY_TYPES.get(item.memory_type, item.memory_type)
            lines.append(f"[{type_label}] {item.content}")
        return "\n".join(lines)

    raise ValueError(f"Неизвестный инструмент: {name!r}")
