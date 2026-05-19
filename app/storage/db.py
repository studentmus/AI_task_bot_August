import logging
from pathlib import Path

from sqlalchemy import Boolean, Column, Integer, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


logger = logging.getLogger(__name__)


def _engine():
    db_file = Path(settings.db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_file.resolve()}"
    return create_engine(url, connect_args={"check_same_thread": False, "timeout": 15})


engine = _engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(String, nullable=False)
    category = Column(String, default="inbox")
    priority = Column(String)
    suggested_date = Column(String)
    event_time = Column(String)
    all_day = Column(Boolean, default=True)
    google_event_id = Column(String)
    status = Column(String, default="pending")
    created_at = Column(String)
    telegram_user_id = Column(Integer)
    confirmed_at = Column(String)
    last_ping_at = Column(String)
    ping_count = Column(Integer, default=0)
    urgency     = Column(Integer, nullable=True)   # 1-5: how time-sensitive
    importance  = Column(Integer, nullable=True)   # 1-5: how impactful


class MemoryItem(Base):
    __tablename__ = "memory_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    content = Column(String, nullable=False)
    memory_type = Column(String, nullable=False)
    status = Column(String, default="proposed")  # proposed / confirmed / rejected
    created_at = Column(String)
    confirmed_at = Column(String)


class LogEntry(Base):
    __tablename__ = "log_entries"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    user_id         = Column(Integer, nullable=False, index=True)
    sphere          = Column(String, nullable=False, index=True)   # canonical: sleep, nutrition…
    raw_text        = Column(String, nullable=False)
    logged_at       = Column(String, nullable=False, index=True)   # "YYYY-MM-DD HH:MM" user TZ
    created_at      = Column(String, nullable=False)
    structured_data = Column(String, nullable=True)                # JSON blob, populated async


class DialogMessage(Base):
    __tablename__ = "dialog_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    role = Column(String, nullable=False)      # "user" | "assistant"
    content = Column(String, nullable=False)
    created_at = Column(String, nullable=False)


class RecurringTask(Base):
    __tablename__ = "recurring_tasks"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id = Column(Integer, nullable=False, index=True)
    text             = Column(String, nullable=False)
    event_time       = Column(String, nullable=True)   # "09:00" | "09:00-10:00" | None
    recurrence       = Column(String, nullable=False)  # "daily" | "weekdays" | "weekly:N" (N=0..6)
    start_date       = Column(String, nullable=False)  # YYYY-MM-DD
    end_date         = Column(String, nullable=True)   # YYYY-MM-DD | None = indefinite
    active           = Column(Boolean, default=True)
    created_at       = Column(String, nullable=False)


class ToolAuditLog(Base):
    __tablename__ = "tool_audit_log"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    created_at  = Column(String, nullable=False, index=True)
    user_id     = Column(Integer, nullable=False, index=True)
    tool_name   = Column(String, nullable=False)
    args_json   = Column(String)
    ok          = Column(Boolean, nullable=False)
    result_text = Column(String)
    error_text  = Column(String)


def _apply_migrations() -> None:
    """Добавляет/переименовывает колонки в существующей БД (idempotent)."""
    insp = inspect(engine)
    existing = {col["name"] for col in insp.get_columns("tasks")}
    pending = []

    if "priority" not in existing:
        pending.append("ALTER TABLE tasks ADD COLUMN priority TEXT")
    if "last_ping_at" not in existing:
        pending.append("ALTER TABLE tasks ADD COLUMN last_ping_at TEXT")
    if "ping_count" not in existing:
        pending.append("ALTER TABLE tasks ADD COLUMN ping_count INTEGER DEFAULT 0")
    if "radicale_uid" in existing and "google_event_id" not in existing:
        pending.append("ALTER TABLE tasks RENAME COLUMN radicale_uid TO google_event_id")
    if "urgency" not in existing:
        pending.append("ALTER TABLE tasks ADD COLUMN urgency INTEGER")
    if "importance" not in existing:
        pending.append("ALTER TABLE tasks ADD COLUMN importance INTEGER")
    # Clear legacy string importance values ("medium", "high", etc.) that break numeric priority
    pending.append("UPDATE tasks SET importance = NULL WHERE importance IN ('low','medium','high','critical')")

    if "log_entries" in insp.get_table_names():
        existing_log = {col["name"] for col in insp.get_columns("log_entries")}
        if "structured_data" not in existing_log:
            pending.append("ALTER TABLE log_entries ADD COLUMN structured_data TEXT")

    # recurring_tasks table is created by Base.metadata.create_all, but if DB
    # predates this feature we still need it. create_all handles new tables safely.

    if not pending:
        return

    with engine.connect() as conn:
        for sql in pending:
            logger.info("DB migration: %s", sql)
            conn.execute(text(sql))
        conn.commit()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _apply_migrations()
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.commit()
