import json
import logging

from sqlalchemy.orm import Session

from app.domain.task_actions import TaskActions
from app.domain.task_resolution import format_candidates, resolve_task_reference
from app.storage.task_repo import TaskRepo


logger = logging.getLogger(__name__)


async def execute_tool_call(tool_call: dict, session: Session, user_id: int) -> str:
    """Выполняет один tool call от LLM.

    Принимает tool_call в формате OpenAI/DeepSeek:
        {"id": "...", "function": {"name": "...", "arguments": "{...}"}}

    Возвращает строку-результат для role=tool ответа.
    """
    try:
        name = tool_call["function"]["name"]
        args = json.loads(tool_call["function"]["arguments"])
    except (KeyError, json.JSONDecodeError) as exc:
        logger.warning("Malformed tool_call: %s", exc)
        return _err(f"Неверный формат вызова инструмента: {exc}")

    logger.info("Tool call: %s args=%s", name, args)

    try:
        result = await _dispatch(name, args, session, user_id)
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


def _resolve_task_ids(args: dict, session: Session, user_id: int) -> list[int]:
    """Возвращает список ID из батч-аргумента task_ids или одиночного task_id/task_text."""
    ids = args.get("task_ids")
    if ids:
        return [int(i) for i in ids]
    return [_resolve_task_id(args, session, user_id)]


async def _dispatch(name: str, args: dict, session: Session, user_id: int) -> str:
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
        # Задача создана через LLM = пользователь уже подтвердил намерение.
        # Сразу переводим в confirmed, чтобы она не была уязвима для /cleanup
        # и гарантированно отображалась в утреннем плане и get_upcoming_tasks.
        repo.confirm(task_id)
        session.commit()
        logger.info("Tool create_task → id=%s (confirmed)", task_id)

        synced = False
        task = repo.get(task_id)
        if task is not None:
            try:
                from app.domain.google_calendar import create_event
                event_id = create_event(task)
                if event_id:
                    repo.mark_synced(task_id, event_id)
                    session.commit()
                    synced = True
            except Exception:
                logger.exception("Google Calendar sync failed for task id=%s", task_id)

        cal_note = "" if synced else " (в календарь не добавлена, но сохранена в базе)"
        return f"Задача создана, id={task_id}: «{args['text']}» на {args['date']}{cal_note}"

    if name == "complete_task":
        ids = _resolve_task_ids(args, session, user_id)
        results = [actions.complete_task(tid) for tid in ids]
        return " | ".join(results)

    if name == "delete_task":
        ids = _resolve_task_ids(args, session, user_id)
        results = [actions.delete_task(tid) for tid in ids]
        return " | ".join(results)

    if name == "move_task":
        new_time = args.get("new_time") or None
        task_id = _resolve_task_id(args, session, user_id)
        # new_date опционален: если не передан, берём текущую дату задачи
        new_date = args.get("new_date") or None
        if not new_date:
            task_obj = TaskRepo(session).get(task_id)
            if task_obj is None:
                raise ValueError(f"Задача {task_id} не найдена.")
            new_date = task_obj.suggested_date
        return actions.move_task(
            task_id=task_id,
            new_date=new_date,
            new_time=new_time,
            all_day=new_time is None,
        )

    if name == "snooze_task":
        return actions.snooze_task(
            task_id=_resolve_task_id(args, session, user_id),
            until_date=args["until_date"],
            until_time=args.get("until_time") or None,
        )

    if name == "edit_task_title":
        return actions.edit_task_title(
            task_id=_resolve_task_id(args, session, user_id),
            new_title=args["new_title"].strip(),
        )

    if name == "set_category":
        return actions.set_category(_resolve_task_id(args, session, user_id), args["category"])

    if name == "get_calendar_events":
        import asyncio
        from app.domain.google_calendar import get_upcoming_events, format_events_for_llm
        from zoneinfo import ZoneInfo
        from datetime import datetime
        date_arg = (args.get("date") or "").strip() or None
        days = max(1, min(7, int(args.get("days") or 1)))
        if not date_arg:
            tz = ZoneInfo(__import__("app.config", fromlist=["settings"]).settings.task_timezone)
            date_arg = datetime.now(tz=tz).strftime("%Y-%m-%d")
        try:
            events = await asyncio.to_thread(get_upcoming_events, date_arg, days)
            return format_events_for_llm(events, date_arg)
        except Exception as exc:
            return f"Не удалось прочитать Google Calendar: {exc}"

    if name == "get_today_plan":
        import asyncio
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from app.config import settings as _cfg

        date_arg = args.get("date") or None
        tz = ZoneInfo(_cfg.task_timezone)
        gcal_date = date_arg or datetime.now(tz=tz).strftime("%Y-%m-%d")
        label = date_arg or "сегодня"

        tasks = actions.get_today_plan(user_id, today=date_arg)

        # task lines (include id for LLM reference)
        lines: list[tuple[str, str]] = []  # (sort_key, text)
        for t in tasks:
            tp = t.event_time if (not t.all_day and t.event_time) else "весь день"
            key = t.event_time.split("-")[0].strip() if (not t.all_day and t.event_time) else "99:99"
            lines.append((key, f"[задача id={t.id}] {tp} | [{t.status}] {t.text}"))

        # GCal events (non-bot only)
        try:
            from app.domain.google_calendar import get_upcoming_events
            cal_events = await asyncio.to_thread(get_upcoming_events, gcal_date, 1)
            for e in cal_events:
                if e.is_bot_task:
                    continue
                if e.all_day:
                    tp, key = "весь день", "99:99"
                else:
                    tp = e.start_time + (f"–{e.end_time}" if e.end_time else "")
                    key = e.start_time
                lines.append((key, f"[GCal] {tp} | {e.summary}"))
        except Exception as exc:
            logger.warning("get_today_plan: GCal failed: %s", exc)

        if not lines:
            return f"На {label} ничего нет."

        lines.sort(key=lambda x: x[0])
        return f"На {label}:\n" + "\n".join(t for _, t in lines)

    if name == "analyze_logs":
        from app.domain.log_analytics import analyze_sphere
        from app.storage.log_repo import LogRepo
        sphere = args["sphere"].strip().lower()
        days = int(args.get("days") or 14)
        entries = LogRepo(session).get_recent(user_id, sphere=sphere, days=days, limit=200)
        return analyze_sphere(entries, sphere, days)

    if name == "query_logs":
        from app.storage.log_repo import LogRepo
        sphere = args.get("sphere") or None
        days = int(args.get("days") or 7)
        entries = LogRepo(session).get_recent(user_id, sphere=sphere, days=days, limit=50)
        if not entries:
            label = f"«{sphere}»" if sphere else "всех сфер"
            return f"Нет записей за последние {days} дн. в {label}."
        lines = [f"Записи за {days} дн. ({sphere or 'все сферы'}):"]
        for e in reversed(entries):
            lines.append(f"[{e.logged_at}] {e.sphere}: {e.raw_text}")
        return "\n".join(lines)

    if name == "log_energy":
        from app.llm.obsidian_tools import append_to_bot_log
        level = int(args["level"])
        notes = (args.get("notes") or "").strip()
        level = max(1, min(10, level))
        entry = f"Энергия: {level}/10" + (f" — {notes}" if notes else "")
        await append_to_bot_log("energy.md", entry)
        label = "высокая" if level >= 7 else ("средняя" if level >= 5 else "низкая")
        return f"Состояние записано: {level}/10 ({label})."

    if name == "read_bot_log":
        from app.llm.obsidian_tools import read_bot_log
        return await read_bot_log(args["filename"])

    if name == "save_fact_to_obsidian":
        from app.llm.obsidian_tools import save_fact_to_obsidian
        return await save_fact_to_obsidian(args["fact"].strip())

    if name == "read_memory_from_obsidian":
        from app.llm.obsidian_tools import read_memory_from_obsidian
        return await read_memory_from_obsidian()

    if name == "read_obsidian_protocol":
        from app.llm.obsidian_tools import read_obsidian_protocol
        return await read_obsidian_protocol(args["sphere"])

    if name == "append_obsidian_log":
        from app.llm.obsidian_tools import append_obsidian_log
        return await append_obsidian_log(args["sphere"], args["entry"])

    raise ValueError(f"Неизвестный инструмент: {name!r}")
