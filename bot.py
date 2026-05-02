import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from task_engine import ParseResult, parse_date_input, parse_task, parse_time_input


BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv(
    "AI_TASK_DB",
    "/data/data/com.termux/files/home/ai-stack/bot/tasks.db",
)
SYNC_SCRIPT = os.getenv(
    "AI_TASK_SYNC_SCRIPT",
    "/data/data/com.termux/files/home/ai-stack/bot/sync_to_radicale.py",
)

EDIT_DATE, EDIT_TIME = range(2)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ai-task-bot")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_columns() -> None:
    """Keep the existing SQLite DB compatible with the new bot version."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(tasks)")
    cols = {row["name"] for row in cur.fetchall()}

    migrations = []
    if "status" not in cols:
        migrations.append("ALTER TABLE tasks ADD COLUMN status TEXT DEFAULT 'pending'")
    if "created_at" not in cols:
        migrations.append("ALTER TABLE tasks ADD COLUMN created_at TEXT")
    if "telegram_user_id" not in cols:
        migrations.append("ALTER TABLE tasks ADD COLUMN telegram_user_id INTEGER")
    if "confirmed_at" not in cols:
        migrations.append("ALTER TABLE tasks ADD COLUMN confirmed_at TEXT")
    if "event_time" not in cols:
        migrations.append("ALTER TABLE tasks ADD COLUMN event_time TEXT")
    if "all_day" not in cols:
        migrations.append("ALTER TABLE tasks ADD COLUMN all_day BOOLEAN DEFAULT 1")

    for sql in migrations:
        logger.info("Applying migration: %s", sql)
        cur.execute(sql)

    cur.execute("UPDATE tasks SET all_day = 1 WHERE all_day IS NULL")
    conn.commit()
    conn.close()


def insert_pending_task(result: ParseResult, user_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO tasks (
            text, category, importance, suggested_date, event_time, all_day,
            radicale_uid, status, created_at, telegram_user_id
        )
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            result.clean_text,
            "inbox",
            "medium",
            result.date,
            result.time,
            1 if result.all_day else 0,
            "pending",
            datetime.now().isoformat(timespec="seconds"),
            user_id,
        ),
    )
    task_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return task_id


def fetch_task(task_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, text, suggested_date, event_time, all_day, status, radicale_uid
        FROM tasks
        WHERE id = ?
        """,
        (task_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def confirm_task(task_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE tasks
        SET status = 'confirmed', confirmed_at = ?
        WHERE id = ?
        """,
        (datetime.now().isoformat(timespec="seconds"), task_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def delete_task(task_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def update_task_date(task_id: int, date_str: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET suggested_date = ? WHERE id = ?", (date_str, task_id))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def update_task_time(task_id: int, event_time: Optional[str], all_day: bool) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE tasks SET event_time = ?, all_day = ? WHERE id = ?",
        (event_time, 1 if all_day else 0, task_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def update_task_date_and_maybe_time(task_id: int, parsed: ParseResult) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    if parsed.all_day:
        cur.execute(
            "UPDATE tasks SET suggested_date = ? WHERE id = ?",
            (parsed.date, task_id),
        )
    else:
        cur.execute(
            """
            UPDATE tasks
            SET suggested_date = ?, event_time = ?, all_day = 0
            WHERE id = ?
            """,
            (parsed.date, parsed.time, task_id),
        )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def run_sync() -> tuple[int, str, str]:
    logger.info("Running sync script: %s", SYNC_SCRIPT)
    result = subprocess.run(
        [sys.executable, SYNC_SCRIPT],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def format_date(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekdays = [
            "понедельник",
            "вторник",
            "среда",
            "четверг",
            "пятница",
            "суббота",
            "воскресенье",
        ]
        return f"{dt.strftime('%d.%m.%Y')} ({weekdays[dt.weekday()]})"
    except Exception:
        return date_str


def build_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_{task_id}"),
                InlineKeyboardButton("✏️ Изменить дату", callback_data=f"editdate_{task_id}"),
            ],
            [
                InlineKeyboardButton("🕐 Изменить время", callback_data=f"edittime_{task_id}"),
                InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{task_id}"),
            ],
        ]
    )


def build_card(row: sqlite3.Row, parser_name: Optional[str] = None) -> str:
    event_time = row["event_time"]
    all_day = bool(row["all_day"])
    time_line = "весь день" if all_day or not event_time else event_time

    lines = [
        "📝 Задача",
        f"Название: {row['text']}",
        f"Дата: {format_date(row['suggested_date'])}",
        f"Время: {time_line}",
    ]
    if parser_name:
        lines.append(f"Парсер: {parser_name}")
    lines.append("")
    lines.append("Добавить в календарь?")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Привет. Я AI Task Bot v4.\n\n"
        "Теперь я различаю простые задачи правилами, а сложные фразы отправляю в Ollama.\n\n"
        "Примеры:\n"
        "• завтра в зал\n"
        "• завтра в 3 часа праздник\n"
        "• в пятницу вечером встреча\n"
        "• 4 мая оплатить счёт"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    raw_text = update.message.text.strip()
    user_id = update.effective_user.id if update.effective_user else 0

    try:
        parsed = parse_task(raw_text)
    except Exception:
        logger.exception("Failed to parse task: %r", raw_text)
        await update.message.reply_text(
            "⚠️ Не смог разобрать дату. Попробуй, например: завтра в 3 часа встреча"
        )
        return

    task_id = insert_pending_task(parsed, user_id)
    row = fetch_task(task_id)
    if row is None:
        await update.message.reply_text("⚠️ Задача записалась, но я не смог её прочитать.")
        return

    logger.info(
        "Created pending task id=%s parser=%s date=%s time=%s all_day=%s text=%r",
        task_id,
        parsed.parser,
        parsed.date,
        parsed.time,
        parsed.all_day,
        parsed.clean_text,
    )

    await update.message.reply_text(
        build_card(row, parser_name=parsed.parser),
        reply_markup=build_keyboard(task_id),
    )


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    task_id = int(query.data.split("_", 1)[1])
    row = fetch_task(task_id)
    if row is None:
        await query.edit_message_text("⚠️ Не нашёл задачу для подтверждения.")
        return

    if not confirm_task(task_id):
        await query.edit_message_text("⚠️ Не удалось подтвердить задачу.")
        return

    code, out, err = run_sync()
    reply = "✅ Добавлено в календарь.\n\n" + build_card(row).replace(
        "\nДобавить в календарь?", ""
    )

    if code == 0 and out:
        reply += f"\n\nЛог sync:\n{out}"
    elif code != 0:
        reply += "\n\n⚠️ Ошибка sync."
        if err:
            reply += f"\n{err}"

    await query.edit_message_text(reply)


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    task_id = int(query.data.split("_", 1)[1])
    if delete_task(task_id):
        await query.edit_message_text("❌ Задача отменена и удалена.")
    else:
        await query.edit_message_text("⚠️ Не нашёл задачу для удаления.")


async def edit_date_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data or not query.message:
        return ConversationHandler.END
    await query.answer()

    task_id = int(query.data.split("_", 1)[1])
    context.user_data["edit_task_id"] = task_id
    context.user_data["edit_chat_id"] = query.message.chat_id
    context.user_data["edit_message_id"] = query.message.message_id

    await query.edit_message_text(
        "✏️ Введи новую дату.\n\n"
        "Примеры:\n"
        "• завтра\n"
        "• 4 мая\n"
        "• в понедельник\n"
        "• завтра в 15:00"
    )
    return EDIT_DATE


async def edit_date_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return EDIT_DATE

    task_id = context.user_data.get("edit_task_id")
    if not task_id:
        await update.message.reply_text("⚠️ Не вижу, какую задачу редактировать.")
        return ConversationHandler.END

    raw_text = update.message.text.strip()
    try:
        parsed = parse_date_input(raw_text)
    except Exception:
        logger.exception("Failed to parse edited date: %r", raw_text)
        await update.message.reply_text(
            "⚠️ Не смог разобрать дату. Попробуй: завтра, 4 мая, в понедельник."
        )
        return EDIT_DATE

    if not update_task_date_and_maybe_time(int(task_id), parsed):
        await update.message.reply_text("⚠️ Не смог обновить задачу.")
        return ConversationHandler.END

    row = fetch_task(int(task_id))
    if row is None:
        await update.message.reply_text("⚠️ Задача исчезла из базы.")
        return ConversationHandler.END

    await update.message.reply_text(
        "✅ Дата обновлена.\n\n" + build_card(row),
        reply_markup=build_keyboard(int(task_id)),
    )
    return ConversationHandler.END


async def edit_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data or not query.message:
        return ConversationHandler.END
    await query.answer()

    task_id = int(query.data.split("_", 1)[1])
    context.user_data["edit_task_id"] = task_id
    context.user_data["edit_chat_id"] = query.message.chat_id
    context.user_data["edit_message_id"] = query.message.message_id

    await query.edit_message_text(
        "🕐 Введи новое время.\n\n"
        "Примеры:\n"
        "• 15:00\n"
        "• в 3 часа\n"
        "• утром\n"
        "• днём\n"
        "• вечером\n"
        "• без времени"
    )
    return EDIT_TIME


async def edit_time_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return EDIT_TIME

    task_id = context.user_data.get("edit_task_id")
    if not task_id:
        await update.message.reply_text("⚠️ Не вижу, какую задачу редактировать.")
        return ConversationHandler.END

    raw_text = update.message.text.strip()
    try:
        event_time, all_day = parse_time_input(raw_text)
    except Exception:
        logger.exception("Failed to parse edited time: %r", raw_text)
        await update.message.reply_text(
            "⚠️ Не смог разобрать время. Попробуй: 15:00, утром, вечером, без времени."
        )
        return EDIT_TIME

    if not update_task_time(int(task_id), event_time, all_day):
        await update.message.reply_text("⚠️ Не смог обновить задачу.")
        return ConversationHandler.END

    row = fetch_task(int(task_id))
    if row is None:
        await update.message.reply_text("⚠️ Задача исчезла из базы.")
        return ConversationHandler.END

    await update.message.reply_text(
        "✅ Время обновлено.\n\n" + build_card(row),
        reply_markup=build_keyboard(int(task_id)),
    )
    return ConversationHandler.END


async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("Редактирование отменено.")
    return ConversationHandler.END


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, text, suggested_date, event_time, all_day, status, radicale_uid
        FROM tasks
        ORDER BY id DESC
        LIMIT 10
        """
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("База пока пустая.")
        return

    lines = ["Последние задачи:\n"]
    for row in rows:
        mark = "📅" if row["radicale_uid"] else "🕓"
        time_part = "весь день" if row["all_day"] or not row["event_time"] else row["event_time"]
        lines.append(
            f"{mark} {row['id']} | {row['suggested_date']} | {time_part} | "
            f"{row['status']} | {row['text']}"
        )

    await update.message.reply_text("\n".join(lines))


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не найден в переменных окружения")

    ensure_columns()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pending", pending))

    edit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_date_start, pattern=r"^editdate_\d+$"),
            CallbackQueryHandler(edit_time_start, pattern=r"^edittime_\d+$"),
        ],
        states={
            EDIT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_date_receive)],
            EDIT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_time_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel_edit)],
    )
    app.add_handler(edit_conv)

    app.add_handler(CallbackQueryHandler(confirm_callback, pattern=r"^confirm_\d+$"))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^cancel_\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("bot.py v4 hybrid router started")
    print("🤖 bot.py v4 (hybrid router + editable card) запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
