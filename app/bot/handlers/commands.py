from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.storage.db import SessionLocal
from app.storage.task_repo import TaskRepo


commands_router = Router(name="commands")

_HELP_TEXT = (
    "Привет! Я AI Task Bot.\n\n"
    "── Кнопки ──\n"
    "  📝 Записать →   — выбрать сферу и записать лог\n"
    "  🎯 Что делать?  — рекомендация по энергии и приоритетам\n"
    "  ⚙️ Команды      — задачи, мотивация, отмена, очистка\n\n"
    "── Логирование (slash-команды) ──\n"
    "  /sleep [текст]    — сон (23:30–7:00, с полуночи до 8…)\n"
    "  /meal  [текст]    — питание\n"
    "  /workout [текст]  — тренировка\n"
    "  /german [текст]   — немецкий (/de)\n"
    "  /romanian [текст] — румынский (/ro)\n"
    "  /ideas [текст]    — идеи\n"
    "  /ctx   [текст]    — личный контекст\n"
    "  /wish  [текст]    — список покупок\n"
    "  /guitar [текст]   — игра на гитаре\n"
    "  /stop             — выйти из режима\n"
    "  /undo [сфера]    — отменить последнюю запись\n\n"
    "── Задачи (свободный текст) ──\n"
    "  «завтра в 15:00 встреча» — создать задачу\n"
    "  «разобрать ящик» — без даты → бэклог\n"
    "  /pending   — последние задачи\n"
    "  /recurring — повторяющиеся задачи\n"
    "  /cleanup   — очистить мусор\n\n"
    "── Инструменты ──\n"
    "  /motivate [категория] — мотивационный пинок\n"
    "  Категории: зал, thesis, немецкий, румынский\n"
    "  /audit — последние 10 tool calls (отладка)\n\n"
    "── Примеры ──\n"
    "• /sleep 23:30–7:00, хорошо выспался\n"
    "• /meal каша 300г + 2 яйца\n"
    "• /wish [Техника] AirPods Pro\n"
    "• в пятницу в 15:00 встреча с Ваней"
)


@commands_router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    from app.bot.handlers.log_handler import LOG_KEYBOARD
    await message.answer(_HELP_TEXT, reply_markup=LOG_KEYBOARD)


def _task_priority_score(task) -> int:
    try:
        return int(getattr(task, "urgency", None) or 0) * int(getattr(task, "importance", None) or 0)
    except (TypeError, ValueError):
        return 0


def _task_priority_icon(task) -> str:
    score = _task_priority_score(task)
    if score >= 15:
        return "🔴"
    if score >= 8:
        return "🟡"
    if score > 0:
        return "🟢"
    return "  "


@commands_router.message(Command("pending"))
async def cmd_pending(message: Message) -> None:
    with SessionLocal() as session:
        repo = TaskRepo(session)
        tasks = repo.list_recent(limit=15)

    if not tasks:
        await message.answer("Задач пока нет.")
        return

    # Sort: tasks with priority first (desc score), then by id desc
    tasks.sort(key=lambda t: (-_task_priority_score(t), -t.id))

    lines = ["📋 Задачи (по приоритету):\n"]
    for i, task in enumerate(tasks, start=1):
        icon = _task_priority_icon(task)
        date_str = task.suggested_date or "бэклог"
        lines.append(f"{icon} {i}. {date_str} — {task.text} [{task.status}]")

    await message.answer("\n".join(lines))


@commands_router.message(Command("cleanup"))
async def cmd_cleanup(message: Message) -> None:
    """Разовая очистка: мусорные pending-задачи + артефакты ошибок в истории диалога."""
    user_id = message.from_user.id if message.from_user else 0
    with SessionLocal() as session:
        from app.domain.task_service import TaskService
        from app.storage.dialog_repo import DialogRepo
        svc = TaskService(session)
        count_tasks = svc.cleanup_stale_pending(older_than_hours=1)
        count_phantoms = svc.cleanup_query_phantoms(user_id)
        count_history = DialogRepo(session).purge_artifacts(user_id)
        session.commit()

    parts: list[str] = []
    if count_tasks + count_phantoms:
        parts.append(f"{count_tasks + count_phantoms} устаревших/phantom задач")
    if count_history:
        parts.append(f"{count_history} артефактов из истории диалога")
    if parts:
        await message.answer(f"🧹 Очищено: {', '.join(parts)}.")
    else:
        await message.answer("✅ Нечего чистить.")


@commands_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Действие отменено.")


@commands_router.message(Command("audit"))
async def cmd_audit(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    with SessionLocal() as session:
        from app.storage.db import ToolAuditLog
        rows = (
            session.query(ToolAuditLog)
            .filter(ToolAuditLog.user_id == user_id)
            .order_by(ToolAuditLog.id.desc())
            .limit(10)
            .all()
        )
    if not rows:
        await message.answer("Аудит пуст — LLM tool calls ещё не было.")
        return

    lines = ["🔍 Последние tool calls:\n"]
    for r in reversed(rows):
        icon = "✅" if r.ok else "❌"
        ts = r.created_at[11:16]
        args_short = (r.args_json or "")[:60]
        lines.append(f"{icon} {ts} {r.tool_name}({args_short})")
        if not r.ok and r.error_text:
            lines.append(f"   ↳ {r.error_text[:80]}")
    await message.answer("\n".join(lines))


@commands_router.message(Command("recurring"))
async def cmd_recurring(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    with SessionLocal() as session:
        from app.storage.recurring_repo import RecurringRepo
        items = RecurringRepo(session).list_active(user_id)

    if not items:
        await message.answer("Повторяющихся задач нет.\n\nСоздать: «напоминай каждый день пить витамины»")
        return

    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    lines = ["🔁 Повторяющиеся задачи:\n"]
    for rt in items:
        recur = {"daily": "ежедневно", "weekdays": "Пн-Пт"}.get(rt.recurrence)
        if recur is None and rt.recurrence.startswith("weekly:"):
            try:
                n = int(rt.recurrence.split(":")[1])
                recur = f"каждый {days_ru[n]}"
            except (ValueError, IndexError):
                recur = rt.recurrence
        end_str = f" до {rt.end_date}" if rt.end_date else ""
        time_str = f" {rt.event_time}" if rt.event_time else ""
        lines.append(f"{rt.id}. {rt.text} — {recur}{time_str}{end_str}")

    await message.answer("\n".join(lines))
