from pathlib import Path

from sqlalchemy import Boolean, Column, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


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
    suggested_date = Column(String)
    event_time = Column(String)
    all_day = Column(Boolean, default=True)
    radicale_uid = Column(String)
    status = Column(String, default="pending")
    created_at = Column(String)
    telegram_user_id = Column(Integer)
    confirmed_at = Column(String)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
