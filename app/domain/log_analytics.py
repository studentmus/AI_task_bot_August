"""Server-side analytics for log_entries.

Each sphere has a typed parser + aggregate function. The LLM receives
pre-computed numbers instead of 50 raw strings to count itself.
"""
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings
from app.storage.db import LogEntry


def _get_structured(entry: LogEntry) -> dict | None:
    if not entry.structured_data:
        return None
    try:
        return json.loads(entry.structured_data)
    except (json.JSONDecodeError, TypeError):
        return None

# ── Sleep parsing ─────────────────────────────────────────────────────────────
# Matches "(7ч 30м)" or "(8ч)" produced by parse_sleep_time in log_handler.py
_DURATION_RE = re.compile(r"\((\d+)ч(?:[.\s]*(\d+)м)?\)")
# Matches "23:30–07:00" to extract start/end even if duration tag is absent
_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})[–\-](\d{1,2}):(\d{2})")


def _parse_sleep_minutes(text: str) -> int | None:
    m = _DURATION_RE.search(text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    # Fallback: compute from HH:MM–HH:MM
    r = _RANGE_RE.search(text)
    if r:
        sh, sm, eh, em = (int(x) for x in r.groups())
        start = sh * 60 + sm
        end = eh * 60 + em
        duration = end - start if end > start else (1440 + end - start)
        if 60 <= duration <= 900:   # sanity: 1h–15h
            return duration
    return None


# ── Shared helpers ────────────────────────────────────────────────────────────

def _today_tz() -> str:
    tz = ZoneInfo(settings.task_timezone)
    return datetime.now(tz=tz).strftime("%Y-%m-%d")


def _group_by_date(entries: list[LogEntry]) -> dict[str, list[LogEntry]]:
    by_date: dict[str, list[LogEntry]] = defaultdict(list)
    for e in entries:
        by_date[e.logged_at[:10]].append(e)
    return dict(by_date)


def _streak(dates: set[str], today: str) -> int:
    """Consecutive days with ≥1 entry, counting backwards from today (or yesterday)."""
    d = datetime.strptime(today, "%Y-%m-%d").date()
    if today not in dates:
        d -= timedelta(days=1)
    count = 0
    while d.strftime("%Y-%m-%d") in dates:
        count += 1
        d -= timedelta(days=1)
    return count


def _last_entries(entries: list[LogEntry], n: int = 3) -> str:
    last = sorted(entries, key=lambda e: e.logged_at, reverse=True)[:n]
    return "\n".join(f"  [{e.logged_at}] {e.raw_text}" for e in last)


def _fmt_min(total_min: int) -> str:
    h, m = divmod(total_min, 60)
    return f"{h}ч {m}м" if m else f"{h}ч"


# ── Per-sphere analyzers ──────────────────────────────────────────────────────

def _analyze_sleep(entries: list[LogEntry], days: int) -> str:
    by_date = _group_by_date(entries)
    today = _today_tz()
    st = _streak(set(by_date), today)
    nights = len(by_date)

    # Prefer structured_data durations, fall back to raw text parsing
    durations: list[int] = []
    for e in entries:
        s = _get_structured(e)
        d = s.get("duration_min") if s else None
        if d is None:
            d = _parse_sleep_minutes(e.raw_text)
        if d is not None:
            durations.append(d)

    lines = [f"📊 Сон за {days} дн. — {nights} ночей записано"]
    if durations:
        avg = int(sum(durations) / len(durations))
        good = sum(1 for d in durations if d >= 420)
        lines += [
            f"Среднее: {_fmt_min(avg)}",
            f"Лучшее: {_fmt_min(max(durations))}  |  Худшее: {_fmt_min(min(durations))}",
            f"Ночей ≥7ч: {good}/{len(durations)}",
        ]
        if len(durations) < nights:
            lines.append(f"(длительность не распознана в {nights - len(durations)} записях)")
    else:
        lines.append("(не удалось извлечь длительность)")

    lines += [f"Streak: {st} дн. подряд", "", "Последние записи:"]
    lines.append(_last_entries(entries))
    return "\n".join(lines)


def _analyze_training(entries: list[LogEntry], days: int) -> str:
    by_date = _group_by_date(entries)
    today = _today_tz()
    st = _streak(set(by_date), today)
    trained_days = len(by_date)

    # Aggregate volume from structured_data when available
    total_volume = 0
    session_types: list[str] = []
    structured_count = 0
    for e in entries:
        s = _get_structured(e)
        if s:
            structured_count += 1
            vol = s.get("total_volume_kg") or 0
            total_volume += vol
            st_type = s.get("session_type")
            if st_type:
                session_types.append(st_type)

    lines = [
        f"📊 Тренировки за {days} дн.",
        f"Сессий: {len(entries)}  |  Дней: {trained_days}/{days}",
        f"Частота: {trained_days/days*100:.0f}% дней",
    ]
    if total_volume > 0:
        lines.append(f"Суммарный объём: {total_volume:,} кг")
    if session_types:
        from collections import Counter
        top = Counter(session_types).most_common(1)[0][0]
        lines.append(f"Тип тренировок: преимущественно {top}")
    lines += [f"Streak: {st} дн. подряд", "", "Последние записи:"]
    lines.append(_last_entries(entries))
    return "\n".join(lines)


def _analyze_nutrition(entries: list[LogEntry], days: int) -> str:
    by_date = _group_by_date(entries)
    today = _today_tz()
    st = _streak(set(by_date), today)
    active_days = len(by_date)

    # Per-day protein and calories from structured_data
    daily_protein: dict[str, float] = {}
    daily_cal: dict[str, float] = {}
    for day, day_entries in by_date.items():
        p_sum = sum(((_get_structured(e) or {}).get("protein_g") or 0) for e in day_entries)
        c_sum = sum(((_get_structured(e) or {}).get("calories") or 0) for e in day_entries)
        if p_sum:
            daily_protein[day] = p_sum
        if c_sum:
            daily_cal[day] = c_sum

    lines = [
        f"📊 Питание за {days} дн.",
        f"Записей: {len(entries)}  |  Активных дней: {active_days}/{days}",
    ]

    if daily_protein:
        avg_p = int(sum(daily_protein.values()) / len(daily_protein))
        max_p = int(max(daily_protein.values()))
        min_p = int(min(daily_protein.values()))
        days_ok = sum(1 for p in daily_protein.values() if p >= 180)
        lines += [
            f"Белок (среднее/день): {avg_p}г  |  макс {max_p}г  мин {min_p}г",
            f"Дней с ≥180г белка: {days_ok}/{len(daily_protein)}",
        ]
    else:
        lines.append("(нутриенты ещё извлекаются — появятся в следующей записи)")

    if daily_cal:
        avg_c = int(sum(daily_cal.values()) / len(daily_cal))
        lines.append(f"Калории (среднее/день): {avg_c} ккал")

    lines += [f"Streak: {st} дн. подряд", "", "Последние записи:"]
    lines.append(_last_entries(entries))
    return "\n".join(lines)


def _analyze_generic(entries: list[LogEntry], sphere: str, days: int) -> str:
    by_date = _group_by_date(entries)
    today = _today_tz()
    st = _streak(set(by_date), today)
    total = len(entries)
    active_days = len(by_date)

    lines = [
        f"📊 {sphere.capitalize()} за {days} дн.",
        f"Записей: {total}  |  Активных дней: {active_days}/{days}",
        f"В среднем: {total/days:.1f} записей/день",
        f"Streak: {st} дн. подряд",
        "",
        "Последние записи:",
    ]
    lines.append(_last_entries(entries))
    return "\n".join(lines)


# ── Public entry point ────────────────────────────────────────────────────────

def analyze_sphere(entries: list[LogEntry], sphere: str, days: int) -> str:
    if not entries:
        return f"Нет данных за {days} дн. в сфере «{sphere}»."
    if sphere == "sleep":
        return _analyze_sleep(entries, days)
    if sphere == "training":
        return _analyze_training(entries, days)
    if sphere == "nutrition":
        return _analyze_nutrition(entries, days)
    return _analyze_generic(entries, sphere, days)
