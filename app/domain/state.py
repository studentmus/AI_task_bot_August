"""Current user state: energy level from explicit input or inferred from sleep.

Priority:
  1. Explicit energy log today (from ping answer or log_energy tool)
  2. Inferred from last night's sleep duration
  3. Unknown
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import settings
from app.storage.db import LogEntry

logger = logging.getLogger(__name__)


@dataclass
class StateContext:
    energy: int | None = None           # 1-10
    energy_label: str = "неизвестна"    # высокая / средняя / низкая / неизвестна
    energy_source: str = "unknown"      # explicit / inferred / unknown
    sleep_min: int | None = None        # last night's sleep in minutes
    notes: str = ""


def _label(e: int | None) -> str:
    if e is None:
        return "неизвестна"
    if e >= 7:
        return "высокая"
    if e >= 5:
        return "средняя"
    return "низкая"


def _get_structured(entry: LogEntry) -> dict:
    try:
        return json.loads(entry.structured_data or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _get_last_sleep(session: Session, user_id: int, today: str) -> int | None:
    """Return duration_min from the most recent sleep entry (today or yesterday)."""
    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    entry = (
        session.query(LogEntry)
        .filter(
            LogEntry.user_id == user_id,
            LogEntry.sphere == "sleep",
            LogEntry.logged_at >= yesterday + " 00:00",
        )
        .order_by(LogEntry.logged_at.desc())
        .first()
    )
    if entry:
        return _get_structured(entry).get("duration_min")
    return None


def _infer_from_sleep(sleep_min: int) -> int:
    """Map sleep duration → rough energy score."""
    if sleep_min >= 480:   # ≥8ч
        return 8
    if sleep_min >= 420:   # 7-8ч
        return 7
    if sleep_min >= 360:   # 6-7ч
        return 5
    if sleep_min >= 300:   # 5-6ч
        return 3
    return 2               # <5ч


def get_current_state(session: Session, user_id: int) -> StateContext:
    tz = ZoneInfo(settings.task_timezone)
    today = datetime.now(tz=tz).strftime("%Y-%m-%d")

    # 1. Explicit energy entry today
    entry = (
        session.query(LogEntry)
        .filter(
            LogEntry.user_id == user_id,
            LogEntry.sphere == "energy",
            LogEntry.logged_at >= today + " 00:00",
        )
        .order_by(LogEntry.logged_at.desc())
        .first()
    )

    sleep_min = _get_last_sleep(session, user_id, today)

    if entry:
        s = _get_structured(entry)
        energy = s.get("energy")
        if isinstance(energy, (int, float)) and 1 <= energy <= 10:
            return StateContext(
                energy=int(energy),
                energy_label=_label(int(energy)),
                energy_source="explicit",
                sleep_min=sleep_min,
                notes=s.get("notes") or "",
            )

    # 2. Infer from sleep
    if sleep_min is not None:
        inferred = _infer_from_sleep(sleep_min)
        h, m = divmod(sleep_min, 60)
        return StateContext(
            energy=inferred,
            energy_label=_label(inferred),
            energy_source="inferred",
            sleep_min=sleep_min,
            notes=f"инференс по сну {h}ч {m}м",
        )

    return StateContext()
