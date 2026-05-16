"""Per-sphere alert rules.

Each check function queries log_entries and returns a list of Alert objects.
Severity: "warning" (red flag) | "info" (nudge).
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import settings
from app.storage.db import LogEntry


@dataclass
class Alert:
    sphere: str
    severity: str   # "warning" | "info"
    key: str        # machine-readable, e.g. "protein_low_2d"
    summary: str    # human-readable data for LLM context


def _today(tz: ZoneInfo) -> str:
    return datetime.now(tz=tz).strftime("%Y-%m-%d")


def _days_back(tz: ZoneInfo, n: int) -> str:
    return (datetime.now(tz=tz) - timedelta(days=n)).strftime("%Y-%m-%d")


def _get_entries(session: Session, user_id: int, sphere: str, since: str) -> list[LogEntry]:
    return (
        session.query(LogEntry)
        .filter(
            LogEntry.user_id == user_id,
            LogEntry.sphere == sphere,
            LogEntry.logged_at >= since,
        )
        .order_by(LogEntry.logged_at.desc())
        .all()
    )


def _days_with_entries(entries: list[LogEntry]) -> set[str]:
    return {e.logged_at[:10] for e in entries}


def _structured(entry: LogEntry) -> dict:
    if not entry.structured_data:
        return {}
    try:
        return json.loads(entry.structured_data)
    except (json.JSONDecodeError, TypeError):
        return {}


# ── Nutrition ─────────────────────────────────────────────────────────────────

PROTEIN_TARGET = 200   # г/день (из протокола)
PROTEIN_LOW    = 150   # порог «мало» (75% от цели)


def check_nutrition(session: Session, user_id: int) -> list[Alert]:
    tz = ZoneInfo(settings.task_timezone)
    today = _today(tz)
    alerts: list[Alert] = []

    # Check last 3 days for protein
    daily_protein: dict[str, float] = {}
    for d in range(3):
        day = _days_back(tz, d)
        since = day + " 00:00"
        until = day + " 23:59"
        entries = (
            session.query(LogEntry)
            .filter(
                LogEntry.user_id == user_id,
                LogEntry.sphere == "nutrition",
                LogEntry.logged_at >= since,
                LogEntry.logged_at <= until,
            )
            .all()
        )
        total = sum((_structured(e).get("protein_g") or 0) for e in entries)
        daily_protein[day] = total

    days_with_data = {d: p for d, p in daily_protein.items() if p > 0}
    low_days = [d for d, p in daily_protein.items() if 0 < p < PROTEIN_LOW]
    zero_days = [d for d, p in daily_protein.items() if p == 0]

    if len(low_days) >= 2:
        avg = int(sum(daily_protein[d] for d in low_days) / len(low_days))
        alerts.append(Alert(
            sphere="nutrition", severity="warning", key="protein_low_2d",
            summary=f"Белок ниже {PROTEIN_LOW}г уже {len(low_days)} дня подряд. "
                    f"Среднее: {avg}г/день. Цель: {PROTEIN_TARGET}г.",
        ))
    elif low_days:
        avg = int(daily_protein[low_days[0]])
        alerts.append(Alert(
            sphere="nutrition", severity="info", key="protein_low_1d",
            summary=f"Сегодня белка мало: {avg}г (цель {PROTEIN_TARGET}г).",
        ))

    if len(zero_days) >= 2:
        alerts.append(Alert(
            sphere="nutrition", severity="warning", key="nutrition_no_log_2d",
            summary=f"Питание не логировалось {len(zero_days)} дня подряд.",
        ))

    return alerts


# ── Training ──────────────────────────────────────────────────────────────────

def check_training(session: Session, user_id: int) -> list[Alert]:
    tz = ZoneInfo(settings.task_timezone)
    alerts: list[Alert] = []

    # Consecutive days without training (check last 7 days)
    since = _days_back(tz, 7) + " 00:00"
    entries = _get_entries(session, user_id, "training", since)
    days_trained = _days_with_entries(entries)

    consecutive_rest = 0
    for d in range(7):
        day = _days_back(tz, d)
        if day in days_trained:
            break
        consecutive_rest += 1

    if consecutive_rest >= 3:
        alerts.append(Alert(
            sphere="training", severity="warning", key="training_skip_3d",
            summary=f"Тренировок не было {consecutive_rest} дней подряд.",
        ))
    elif consecutive_rest == 2:
        alerts.append(Alert(
            sphere="training", severity="info", key="training_skip_2d",
            summary="Тренировок не было 2 дня подряд.",
        ))

    # Weekly frequency (current week Mon-Sun)
    week_start = (datetime.now(tz=tz) - timedelta(days=datetime.now(tz=tz).weekday())).strftime("%Y-%m-%d")
    week_entries = [e for e in entries if e.logged_at[:10] >= week_start]
    week_days = _days_with_entries(week_entries)
    weekday = datetime.now(tz=tz).weekday()  # 0=Mon, 6=Sun

    # If past Wednesday (weekday >= 3) and only 0 training days this week
    if weekday >= 3 and len(week_days) == 0:
        alerts.append(Alert(
            sphere="training", severity="warning", key="training_zero_this_week",
            summary=f"Ни одной тренировки на этой неделе (сегодня {['пн','вт','ср','чт','пт','сб','вс'][weekday]}).",
        ))

    return alerts


# ── Sleep ─────────────────────────────────────────────────────────────────────

SLEEP_MIN_OK = 420   # 7 часов в минутах
SLEEP_LATE_BED = 24 * 60  # позже полуночи


def check_sleep(session: Session, user_id: int) -> list[Alert]:
    tz = ZoneInfo(settings.task_timezone)
    alerts: list[Alert] = []

    since = _days_back(tz, 3) + " 00:00"
    entries = _get_entries(session, user_id, "sleep", since)

    short_nights: list[tuple[str, int]] = []
    late_nights: list[str] = []

    for e in entries:
        s = _structured(e)
        dur = s.get("duration_min")
        bed = s.get("bedtime")
        day = e.logged_at[:10]

        if dur is not None and dur < SLEEP_MIN_OK:
            short_nights.append((day, dur))

        if bed:
            h, m = (int(x) for x in bed.split(":"))
            bed_min = h * 60 + m
            # Treat 0:00-4:00 as late (after midnight)
            if 0 <= bed_min <= 240:
                late_nights.append(day)

    if len(short_nights) >= 2:
        avg = int(sum(d for _, d in short_nights) / len(short_nights))
        h, m = divmod(avg, 60)
        alerts.append(Alert(
            sphere="sleep", severity="warning", key="sleep_short_2n",
            summary=f"Короткий сон {len(short_nights)} ночи подряд. "
                    f"Среднее: {h}ч {m}м (цель ≥7ч).",
        ))
    elif short_nights:
        day, dur = short_nights[0]
        h, m = divmod(dur, 60)
        alerts.append(Alert(
            sphere="sleep", severity="info", key="sleep_short_1n",
            summary=f"Прошлой ночью поспал только {h}ч {m}м.",
        ))

    if late_nights:
        alerts.append(Alert(
            sphere="sleep", severity="info", key="sleep_late_bed",
            summary=f"Лёг после полуночи {len(late_nights)} раз за последние дни.",
        ))

    return alerts


# ── German ────────────────────────────────────────────────────────────────────

def check_german(session: Session, user_id: int) -> list[Alert]:
    tz = ZoneInfo(settings.task_timezone)
    alerts: list[Alert] = []

    since = _days_back(tz, 5) + " 00:00"
    entries = _get_entries(session, user_id, "german", since)
    days_active = _days_with_entries(entries)

    # Days since last entry
    gap = 0
    for d in range(5):
        if _days_back(tz, d) in days_active:
            break
        gap += 1

    if gap >= 3:
        alerts.append(Alert(
            sphere="german", severity="warning", key="german_gap_3d",
            summary=f"Немецкий не практиковался {gap} дней.",
        ))
    elif gap == 2:
        alerts.append(Alert(
            sphere="german", severity="info", key="german_gap_2d",
            summary="Немецкий не практиковался 2 дня.",
        ))

    return alerts


# ── Romanian ─────────────────────────────────────────────────────────────────

def check_romanian(session: Session, user_id: int) -> list[Alert]:
    tz = ZoneInfo(settings.task_timezone)
    alerts: list[Alert] = []

    since = _days_back(tz, 7) + " 00:00"
    entries = _get_entries(session, user_id, "romanian", since)
    days_active = _days_with_entries(entries)

    gap = 0
    for d in range(7):
        if _days_back(tz, d) in days_active:
            break
        gap += 1

    if gap >= 5:
        alerts.append(Alert(
            sphere="romanian", severity="warning", key="romanian_gap_5d",
            summary=f"Румынский не практиковался {gap} дней. Дедлайн по гражданству реальный.",
        ))
    elif gap >= 3:
        alerts.append(Alert(
            sphere="romanian", severity="info", key="romanian_gap_3d",
            summary=f"Румынский не практиковался {gap} дня.",
        ))

    return alerts


# ── Entry point ───────────────────────────────────────────────────────────────

def run_all_checks(session: Session, user_id: int) -> list[Alert]:
    alerts: list[Alert] = []
    for fn in (check_nutrition, check_training, check_sleep, check_german, check_romanian):
        try:
            alerts.extend(fn(session, user_id))
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Alert check failed: %s", fn.__name__)
    return alerts
