import logging
import os
import sqlite3
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


DB_PATH = os.getenv(
    "AI_TASK_DB",
    "/data/data/com.termux/files/home/ai-stack/bot/tasks.db",
)
RADICALE_CALENDAR_DIR = os.getenv(
    "RADICALE_CALENDAR_DIR",
    "/data/data/com.termux/files/home/ai-stack/radicale/collections/collection-root/layash/main",
)
TASK_TIMEZONE = os.getenv("AI_TASK_TIMEZONE", "Europe/Copenhagen")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("sync-to-radicale")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_runtime_columns() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(tasks)")
    cols = {row["name"] for row in cur.fetchall()}
    if "event_time" not in cols:
        cur.execute("ALTER TABLE tasks ADD COLUMN event_time TEXT")
    if "all_day" not in cols:
        cur.execute("ALTER TABLE tasks ADD COLUMN all_day BOOLEAN DEFAULT 1")
    conn.commit()
    conn.close()


def escape_ical_text(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def fold_ical_line(line: str) -> str:
    """Fold long iCalendar lines at 75 bytes, preserving UTF-8 characters."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line

    parts = []
    current = ""
    for char in line:
        candidate = current + char
        limit = 75 if not parts else 74
        if len(candidate.encode("utf-8")) > limit:
            parts.append(current)
            current = char
        else:
            current = candidate
    if current:
        parts.append(current)

    return "\r\n ".join(parts)


def format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def format_wall_time_as_z(dt: datetime) -> str:
    """
    Match the requested output format: DTSTART:20260429T150000Z.

    Strict iCalendar semantics treat trailing Z as UTC. The task manager stores
    event_time as the user's wall-clock time, and the requested Radicale output
    wants that wall-clock time serialized with Z.
    """
    return dt.strftime("%Y%m%dT%H%M%SZ")


def parse_local_datetime(date_str: str, event_time: str) -> datetime:
    local_tz = ZoneInfo(TASK_TIMEZONE)
    parsed_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    hour, minute = map(int, event_time.split(":"))
    return datetime.combine(parsed_date, time(hour, minute), tzinfo=local_tz)


def build_ics(row: sqlite3.Row, uid: str) -> str:
    summary = escape_ical_text(row["text"])
    date_str = row["suggested_date"]
    event_time = row["event_time"]
    all_day = bool(row["all_day"]) or not event_time
    now_utc = datetime.now(timezone.utc)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AI Task Manager//Telegram Bot//RU",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{format_utc(now_utc)}",
        f"SUMMARY:{summary}",
    ]

    if all_day:
        start = datetime.strptime(date_str, "%Y-%m-%d").date()
        end = start + timedelta(days=1)
        lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}")
    else:
        start_local = parse_local_datetime(date_str, event_time)
        end_local = start_local + timedelta(hours=1)
        lines.append(f"DTSTART:{format_wall_time_as_z(start_local)}")
        lines.append(f"DTEND:{format_wall_time_as_z(end_local)}")

    lines.extend(
        [
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )

    folded = [fold_ical_line(line) for line in lines]
    return "\r\n".join(folded) + "\r\n"


def fetch_tasks_to_sync() -> list[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, text, suggested_date, event_time, all_day
        FROM tasks
        WHERE status = 'confirmed'
          AND (radicale_uid IS NULL OR radicale_uid = '')
        ORDER BY id ASC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def mark_synced(task_id: int, uid: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET radicale_uid = ? WHERE id = ?", (uid, task_id))
    conn.commit()
    conn.close()


def sync_one(row: sqlite3.Row, calendar_dir: Path) -> str:
    uid = f"{uuid.uuid4()}@ai-task-manager"
    ics = build_ics(row, uid)
    path = calendar_dir / f"{uid}.ics"
    path.write_text(ics, encoding="utf-8", newline="")
    mark_synced(int(row["id"]), uid)
    logger.info("Synced task id=%s uid=%s path=%s", row["id"], uid, path)
    return uid


def main() -> None:
    ensure_runtime_columns()
    calendar_dir = Path(RADICALE_CALENDAR_DIR)
    calendar_dir.mkdir(parents=True, exist_ok=True)

    rows = fetch_tasks_to_sync()
    if not rows:
        print("Нет новых подтверждённых задач для синхронизации.")
        return

    synced = []
    for row in rows:
        try:
            synced.append(sync_one(row, calendar_dir))
        except Exception:
            logger.exception("Failed to sync task id=%s", row["id"])
            raise

    print(f"Синхронизировано задач: {len(synced)}")


if __name__ == "__main__":
    main()
