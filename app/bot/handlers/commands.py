from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.storage.db import SessionLocal
from app.storage.task_repo import TaskRepo


commands_router = Router(name="commands")


@commands_router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я AI Task Bot.\n\n"
        "Просто напиши задачу — я разберу дату и время сам.\n\n"
        "Примеры:\n"
        "• завтра в зал\n"
        "• в пятницу в 15:00 встреча\n"
        "• 4 мая оплатить счёт\n"
        "• через 3 дня вечером позвонить врачу\n\n"
        "Команды:\n"
        "/pending — последние задачи\n"
        "/cancel  — отменить текущий ввод"
    )


@commands_router.message(Command("pending"))
async def cmd_pending(message: Message) -> None:
    with SessionLocal() as session:
        repo = TaskRepo(session)
        tasks = repo.list_recent(limit=10)

    if not tasks:
        await message.answer("Задач пока нет.")
        return

    lines = ["📋 Последние задачи:\n"]
    for i, task in enumerate(tasks, start=1):
        mark = "✓" if task.status == "confirmed" else " "
        status = task.status.ljust(9)
        lines.append(f"{i}. {task.suggested_date} | {status} | {mark} {task.text}")

    await message.answer("\n".join(lines))


@commands_router.message(Command("cleanup"))
async def cmd_cleanup(message: Message) -> None:
    """Разовая очистка: мусорные pending-задачи + артефакты ошибок в истории диалога."""
    user_id = message.from_user.id if message.from_user else 0
    with SessionLocal() as session:
        from app.domain.task_service import TaskService
        from app.storage.dialog_repo import DialogRepo
        count_tasks = TaskService(session).cleanup_stale_pending(older_than_hours=1)
        count_history = DialogRepo(session).purge_artifacts(user_id)
        session.commit()

    parts: list[str] = []
    if count_tasks:
        parts.append(f"{count_tasks} устаревших задач")
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
