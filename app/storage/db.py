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
    return create_engine(url, connect_args={"check_same_thread": False})


engine = _engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(String, nullable=False)
    category = Column(String, default="inbox")
    importance = Column(String, default="medium")
    priority = Column(String)
    suggested_date = Column(String)
    event_time = Column(String)
    all_day = Column(Boolean, default=True)
    radicale_uid = Column(String)
    status = Column(String, default="pending")
    created_at = Column(String)
    telegram_user_id = Column(Integer)
    confirmed_at = Column(String)
    last_ping_at = Column(String)
    ping_count = Column(Integer, default=0)


def _apply_migrations() -> None:
    """Добавляет новые колонки в существующую БД (idempotent)."""
    existing = {col["name"] for col in inspect(engine).get_columns("tasks")}
    pending = []
    if "priority" not in existing:
        pending.append("ALTER TABLE tasks ADD COLUMN priority TEXT")
    if "last_ping_at" not in existing:
        pending.append("ALTER TABLE tasks ADD COLUMN last_ping_at TEXT")
    if "ping_count" not in existing:
        pending.append("ALTER TABLE tasks ADD COLUMN ping_count INTEGER DEFAULT 0")

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
