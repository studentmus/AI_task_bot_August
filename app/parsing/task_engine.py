import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings


logger = logging.getLogger(__name__)


@dataclass
class ParseResult:
    date: str
    time: Optional[str]
    all_day: bool
    clean_text: str
    parser: str


MONTHS = {
    "января": 1,
    "январь": 1,
    "февраля": 2,
    "февраль": 2,
    "марта": 3,
    "март": 3,
    "апреля": 4,
    "апрель": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июля": 7,
    "июль": 7,
    "августа": 8,
    "август": 8,
    "сентября": 9,
    "сентябрь": 9,
    "октября": 10,
    "октябрь": 10,
    "ноября": 11,
    "ноябрь": 11,
    "декабря": 12,
    "декабрь": 12,
}

WEEKDAYS = {
    "понедельник": 0,
    "понедельника": 0,
    "вторник": 1,
    "вторника": 1,
    "среду": 2,
    "среда": 2,
    "среды": 2,
    "четверг": 3,
    "четверга": 3,
    "пятницу": 4,
    "пятница": 4,
    "пятницы": 4,
    "субботу": 5,
    "суббота": 5,
    "субботы": 5,
    "воскресенье": 6,
    "воскресенья": 6,
}

TIME_WORDS = {
    "утром": "09:00",
    "с утра": "09:00",
    "днём": "14:00",
    "днем": "14:00",
    "после обеда": "14:00",
    "вечером": "19:00",
    "ночью": "22:00",
}


def today_local() -> date:
    try:
        return datetime.now(ZoneInfo(settings.task_timezone)).date()
    except Exception:
        return date.today()


def cleanup_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text, flags=re.UNICODE).strip(" ,.!?;:-\n\t")
    return text or "Без названия"


def remove_span(text: str, start: int, end: int) -> str:
    return cleanup_text((text[:start] + " " + text[end:]).strip())


def next_weekday(base: date, target_weekday: int) -> date:
    delta = (target_weekday - base.weekday()) % 7
    if delta == 0:
        delta = 7
    return base + timedelta(days=delta)


def has_time_signal(text: str) -> bool:
    lower = text.lower()
    explicit_patterns = [
        r"\b\d{1,2}[:.]\d{2}\b",
        r"\b(?:в|к)\s*\d{1,2}\s*(?:час|часа|часов|ч)?\b",
        r"\b\d{1,2}\s*(?:час|часа|часов)\b",
        r"\b\d{1,2}\s*(?:am|pm)\b",
    ]
    if any(re.search(p, lower, flags=re.IGNORECASE) for p in explicit_patterns):
        return True
    if any(word in lower for word in TIME_WORDS):
        return True
    if re.search(
        r"\bчерез\s+\d+\s+(?:день|дня|дней|неделю|недели|недель|месяц|месяца|месяцев|год|года|лет)\b",
        lower,
    ):
        return True
    return False


def route_task(text: str) -> str:
    return "llm" if has_time_signal(text) else "rule"


def parse_rule_based(text: str, base: Optional[date] = None) -> Optional[ParseResult]:
    base = base or today_local()
    lower = text.lower()

    for pattern, delta in [
        (r"\bсегодня\b", 0),
        (r"\bзавтра\b", 1),
        (r"\bпослезавтра\b", 2),
    ]:
        m = re.search(pattern, lower)
        if m:
            return ParseResult(
                date=(base + timedelta(days=delta)).isoformat(),
                time=None,
                all_day=True,
                clean_text=remove_span(text, m.start(), m.end()),
                parser="Rule-Based",
            )

    month_names = "|".join(sorted(MONTHS.keys(), key=len, reverse=True))
    m = re.search(
        rf"\b(?:на\s+)?(\d{{1,2}})\s+({month_names})(?:\s+(\d{{4}}))?\b",
        lower,
        flags=re.IGNORECASE,
    )
    if m:
        day_num = int(m.group(1))
        month_num = MONTHS[m.group(2).lower()]
        year_num = int(m.group(3)) if m.group(3) else base.year
        try:
            target = date(year_num, month_num, day_num)
        except ValueError:
            return None
        if not m.group(3) and target < base:
            target = date(base.year + 1, month_num, day_num)
        return ParseResult(
            date=target.isoformat(),
            time=None,
            all_day=True,
            clean_text=remove_span(text, m.start(), m.end()),
            parser="Rule-Based",
        )

    m = re.search(r"\b(?:на\s+)?(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", lower)
    if m:
        day_num, month_num = int(m.group(1)), int(m.group(2))
        year_raw = m.group(3)
        year_num = int(year_raw) + (2000 if year_raw and int(year_raw) < 100 else 0) if year_raw else base.year
        try:
            target = date(year_num, month_num, day_num)
        except ValueError:
            return None
        if not year_raw and target < base:
            target = date(base.year + 1, month_num, day_num)
        return ParseResult(
            date=target.isoformat(),
            time=None,
            all_day=True,
            clean_text=remove_span(text, m.start(), m.end()),
            parser="Rule-Based",
        )

    # "в следующий/следующую/следующее понедельник/пятницу/..."
    weekday_names = "|".join(sorted(WEEKDAYS.keys(), key=len, reverse=True))
    m = re.search(
        rf"\b(?:в|во|на)\s+(?:следующ\w*\s+)?({weekday_names})\b",
        lower,
        flags=re.IGNORECASE,
    )
    if m:
        return ParseResult(
            date=next_weekday(base, WEEKDAYS[m.group(1).lower()]).isoformat(),
            time=None,
            all_day=True,
            clean_text=remove_span(text, m.start(), m.end()),
            parser="Rule-Based",
        )

    return None


def extract_time_heuristic(text: str) -> Optional[str]:
    lower = text.lower()
    for word, t in TIME_WORDS.items():
        if word in lower:
            return t
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", lower)
    if m:
        return validate_time(f"{int(m.group(1)):02d}:{int(m.group(2)):02d}")
    m = re.search(r"\b(?:в|к)?\s*([0-2]?\d)\s*(?:час|часа|часов|ч)\b", lower)
    if m:
        hour = int(m.group(1))
        if 1 <= hour <= 6 and not any(w in lower for w in ["утра", "ноч", "am"]):
            hour += 12
        if 0 <= hour <= 23:
            return f"{hour:02d}:00"
    return None


def remove_time_words(text: str) -> str:
    patterns = [
        r"\b(?:в|к)\s*\d{1,2}[:.]\d{2}\b",
        r"\b\d{1,2}[:.]\d{2}\b",
        r"\b(?:в|к)?\s*\d{1,2}\s*(?:час|часа|часов|ч)\b",
        r"\bутром\b",
        r"\bс утра\b",
        r"\bдн[её]м\b",
        r"\bпосле обеда\b",
        r"\bвечером\b",
        r"\bночью\b",
    ]
    result = text
    for p in patterns:
        result = re.sub(p, " ", result, flags=re.IGNORECASE)
    return cleanup_text(result)


def validate_date(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def validate_time(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not m:
        raise ValueError(f"Invalid time: {value!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time: {value!r}")
    return f"{hour:02d}:{minute:02d}"


def fix_ambiguous_short_hour(original_text: str, time_value: Optional[str]) -> Optional[str]:
    """Make 'в 3 часа' → 15:00, not 03:00."""
    if not time_value:
        return time_value
    lower = original_text.lower()
    m = re.search(r"\b(?:в|к)\s*([1-6])\s*(?:час|часа|часов|ч)?\b", lower)
    if not m:
        return time_value
    if any(w in lower for w in ["утра", "ноч", "am"]):
        return time_value
    hour, minute = map(int, time_value.split(":"))
    if hour == int(m.group(1)):
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def _sanitize_llm_clean_text(llm_clean: str, original: str) -> str:
    """Если LLM вернул пустой или мусорный clean_text — берём очищенный оригинал."""
    candidate = cleanup_text(llm_clean)
    if candidate and candidate != "Без названия":
        return candidate
    # LLM вернул мусор — чистим оригинал сами
    fallback = remove_time_words(original)
    return fallback if fallback and fallback != "Без названия" else cleanup_text(original)


def parse_llm(text: str, base: Optional[date] = None) -> ParseResult:
    from app.llm.deepseek_client import call_deepseek_parse
    from app.llm.prompt_builder import build_task_prompt

    base = base or today_local()
    prompt = build_task_prompt(text, base)
    parsed = call_deepseek_parse(prompt)

    date_str = validate_date(parsed["date"])
    time_str = validate_time(parsed.get("time"))
    time_str = fix_ambiguous_short_hour(text, time_str)
    all_day = not bool(time_str)

    clean = _sanitize_llm_clean_text(str(parsed.get("clean_text") or ""), text)

    return ParseResult(
        date=date_str,
        time=time_str,
        all_day=all_day,
        clean_text=clean,
        parser="DeepSeek",
    )


def parse_complex_fallback(text: str, base: Optional[date] = None) -> ParseResult:
    base = base or today_local()

    m = re.search(
        r"\bчерез\s+(\d+)\s+(день|дня|дней|неделю|недели|недель|месяц|месяца|месяцев)\b",
        text.lower(),
    )
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        if unit.startswith("д"):
            target = base + timedelta(days=amount)
        elif unit.startswith("н"):
            target = base + timedelta(weeks=amount)
        else:
            target = base + timedelta(days=30 * amount)
        time_str = extract_time_heuristic(text)
        return ParseResult(
            date=target.isoformat(),
            time=time_str,
            all_day=time_str is None,
            clean_text=remove_time_words(remove_span(text, m.start(), m.end())),
            parser="Fallback",
        )

    simple = parse_rule_based(text, base)
    if simple:
        time_str = extract_time_heuristic(text)
        return ParseResult(
            date=simple.date,
            time=time_str,
            all_day=time_str is None,
            clean_text=remove_time_words(simple.clean_text),
            parser="Fallback",
        )

    time_str = extract_time_heuristic(text)
    return ParseResult(
        date=base.isoformat(),
        time=time_str,
        all_day=time_str is None,
        clean_text=remove_time_words(text),
        parser="Fallback",
    )


def parse_task(text: str, base: Optional[date] = None) -> ParseResult:
    base = base or today_local()
    text = cleanup_text(text)
    route = route_task(text)
    logger.info("Router: %s for %r", route, text)

    if route == "rule":
        result = parse_rule_based(text, base)
        if result:
            return result
        raise ValueError(
            "Не нашёл дату в тексте. Попробуй: завтра, 4 мая, в пятницу, через 3 дня."
        )

    try:
        return parse_llm(text, base)
    except Exception:
        logger.exception("LLM failed, using fallback for %r", text)
        return parse_complex_fallback(text, base)


def parse_date_input(text: str) -> ParseResult:
    return parse_task(text)


def parse_time_input(text: str) -> tuple[Optional[str], bool]:
    lower = cleanup_text(text).lower()
    if lower in {"без времени", "без время", "весь день", "на весь день", "all day", "none", "нет", "-"}:
        return None, True
    time_str = extract_time_heuristic(lower)
    if not time_str:
        raise ValueError(f"Cannot parse time from {text!r}")
    return time_str, False
