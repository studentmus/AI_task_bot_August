import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo


logger = logging.getLogger("task-engine")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
TASK_TIMEZONE = os.getenv("AI_TASK_TIMEZONE", "Europe/Copenhagen")


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


LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "description": "Дата задачи в формате YYYY-MM-DD.",
        },
        "time": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Время в формате HH:MM или null.",
        },
        "all_day": {
            "type": "boolean",
            "description": "true если задача на весь день без конкретного времени.",
        },
        "clean_text": {
            "type": "string",
            "description": "Текст задачи без слов даты и времени.",
        },
    },
    "required": ["date", "time", "all_day", "clean_text"],
    "additionalProperties": False,
}


def today_local() -> date:
    try:
        return datetime.now(ZoneInfo(TASK_TIMEZONE)).date()
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
    if any(re.search(pattern, lower, flags=re.IGNORECASE) for pattern in explicit_patterns):
        return True

    if any(word in lower for word in TIME_WORDS):
        return True

    if re.search(r"\bчерез\s+\d+\s+(?:день|дня|дней|неделю|недели|недель|месяц|месяца|месяцев|год|года|лет)\b", lower):
        return True

    return False


def route_task(text: str) -> str:
    """Return 'rule' for simple all-day tasks, otherwise 'llm'."""
    if has_time_signal(text):
        return "llm"
    return "rule"


def parse_rule_based(text: str, base: Optional[date] = None) -> Optional[ParseResult]:
    base = base or today_local()
    lower = text.lower()

    relative_patterns = [
        (r"\bсегодня\b", 0),
        (r"\bзавтра\b", 1),
        (r"\bпослезавтра\b", 2),
    ]
    for pattern, delta in relative_patterns:
        match = re.search(pattern, lower)
        if match:
            target = base + timedelta(days=delta)
            return ParseResult(
                date=target.isoformat(),
                time=None,
                all_day=True,
                clean_text=remove_span(text, match.start(), match.end()),
                parser="Rule-Based",
            )

    month_names = "|".join(sorted(MONTHS.keys(), key=len, reverse=True))
    month_match = re.search(
        rf"\b(?:на\s+)?(\d{{1,2}})\s+({month_names})(?:\s+(\d{{4}}))?\b",
        lower,
        flags=re.IGNORECASE,
    )
    if month_match:
        day_num = int(month_match.group(1))
        month_num = MONTHS[month_match.group(2).lower()]
        year_num = int(month_match.group(3)) if month_match.group(3) else base.year
        try:
            target = date(year_num, month_num, day_num)
        except ValueError:
            return None
        if not month_match.group(3) and target < base:
            target = date(base.year + 1, month_num, day_num)
        return ParseResult(
            date=target.isoformat(),
            time=None,
            all_day=True,
            clean_text=remove_span(text, month_match.start(), month_match.end()),
            parser="Rule-Based",
        )

    numeric_match = re.search(
        r"\b(?:на\s+)?(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b",
        lower,
    )
    if numeric_match:
        day_num = int(numeric_match.group(1))
        month_num = int(numeric_match.group(2))
        year_raw = numeric_match.group(3)
        if year_raw:
            year_num = int(year_raw)
            if year_num < 100:
                year_num += 2000
        else:
            year_num = base.year
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
            clean_text=remove_span(text, numeric_match.start(), numeric_match.end()),
            parser="Rule-Based",
        )

    weekday_names = "|".join(sorted(WEEKDAYS.keys(), key=len, reverse=True))
    weekday_match = re.search(
        rf"\b(?:в|во|на)\s+({weekday_names})\b",
        lower,
        flags=re.IGNORECASE,
    )
    if weekday_match:
        target = next_weekday(base, WEEKDAYS[weekday_match.group(1).lower()])
        return ParseResult(
            date=target.isoformat(),
            time=None,
            all_day=True,
            clean_text=remove_span(text, weekday_match.start(), weekday_match.end()),
            parser="Rule-Based",
        )

    return None


def build_llm_prompt(text: str, base: date) -> str:
    weekday = base.strftime("%A")
    return f"""
Ты парсер задач для Telegram-бота. Верни только JSON без Markdown.

Сегодня: {base.isoformat()}.
День недели сегодня: {weekday}.
Локальная таймзона пользователя: {TASK_TIMEZONE}.
Язык входа: русский, иногда английский.

Нужно извлечь:
- date: дата задачи в формате YYYY-MM-DD.
- time: время в формате HH:MM или null.
- all_day: true, если конкретного времени нет.
- clean_text: текст задачи без слов даты и времени.

Правила:
- "вечером" = 19:00.
- "утром" = 09:00.
- "днём" или "днем" = 14:00.
- "завтра в 3 часа" = 15:00, если нет слов "утра", "ночи".
- "в пятницу" означает ближайшую будущую пятницу.
- "через N дней" считай от сегодняшней даты.
- Если времени нет, time = null и all_day = true.
- Если время есть, all_day = false.

Вход: {text!r}
""".strip()


def call_ollama(prompt: str) -> dict:
    url = OLLAMA_URL.rstrip("/") + "/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": LLM_SCHEMA,
        "options": {"temperature": 0},
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Older Ollama versions may not accept JSON Schema in `format`.
        logger.warning("Ollama JSON Schema call failed: %s. Falling back to format=json", exc)
        fallback_payload = dict(payload)
        fallback_payload["format"] = "json"
        data = json.dumps(fallback_payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8")

    envelope = json.loads(raw)
    response_text = envelope.get("response", "").strip()
    return json.loads(response_text)


def validate_date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    return parsed.isoformat()


def validate_time(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not match:
        raise ValueError(f"Invalid time: {value!r}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time: {value!r}")
    return f"{hour:02d}:{minute:02d}"


def fix_ambiguous_short_hour(original_text: str, time_value: Optional[str]) -> Optional[str]:
    """Make 'в 3 часа' behave as requested: 15:00, not 03:00."""
    if not time_value:
        return time_value

    lower = original_text.lower()
    match = re.search(r"\b(?:в|к)\s*([1-6])\s*(?:час|часа|часов|ч)?\b", lower)
    if not match:
        return time_value
    if any(word in lower for word in ["утра", "ноч", "am"]):
        return time_value
    hour, minute = map(int, time_value.split(":"))
    typed_hour = int(match.group(1))
    if hour == typed_hour:
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def parse_llm(text: str, base: Optional[date] = None) -> ParseResult:
    base = base or today_local()
    prompt = build_llm_prompt(text, base)
    parsed = call_ollama(prompt)

    date_str = validate_date(parsed["date"])
    time_str = validate_time(parsed.get("time"))
    time_str = fix_ambiguous_short_hour(text, time_str)
    all_day = bool(parsed["all_day"])
    if time_str:
        all_day = False
    else:
        all_day = True

    return ParseResult(
        date=date_str,
        time=time_str,
        all_day=all_day,
        clean_text=cleanup_text(str(parsed.get("clean_text") or text)),
        parser="LLM",
    )


def parse_relative_complex_fallback(text: str, base: date) -> Optional[ParseResult]:
    lower = text.lower()
    match = re.search(
        r"\bчерез\s+(\d+)\s+(день|дня|дней|неделю|недели|недель|месяц|месяца|месяцев)\b",
        lower,
    )
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("д"):
        target = base + timedelta(days=amount)
    elif unit.startswith("н"):
        target = base + timedelta(weeks=amount)
    else:
        target = base + timedelta(days=30 * amount)

    clean = remove_span(text, match.start(), match.end())
    time_str = extract_time_heuristic(text)
    clean = remove_time_words(clean)
    return ParseResult(
        date=target.isoformat(),
        time=time_str,
        all_day=time_str is None,
        clean_text=clean,
        parser="Fallback",
    )


def extract_time_heuristic(text: str) -> Optional[str]:
    lower = text.lower()
    for word, time_value in TIME_WORDS.items():
        if word in lower:
            return time_value

    match = re.search(r"\b(\d{1,2})[:.](\d{2})\b", lower)
    if match:
        return validate_time(f"{int(match.group(1)):02d}:{int(match.group(2)):02d}")

    match = re.search(r"\b(?:в|к)?\s*([0-2]?\d)\s*(?:час|часа|часов|ч)\b", lower)
    if match:
        hour = int(match.group(1))
        if 1 <= hour <= 6 and not any(word in lower for word in ["утра", "ноч", "am"]):
            hour += 12
        if 0 <= hour <= 23:
            return f"{hour:02d}:00"

    return None


def remove_time_words(text: str) -> str:
    result = text
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
    for pattern in patterns:
        result = re.sub(pattern, " ", result, flags=re.IGNORECASE)
    return cleanup_text(result)


def parse_complex_fallback(text: str, base: Optional[date] = None) -> ParseResult:
    base = base or today_local()

    relative = parse_relative_complex_fallback(text, base)
    if relative:
        return relative

    simple = parse_rule_based(text, base)
    if simple:
        time_str = extract_time_heuristic(text)
        clean = remove_time_words(simple.clean_text)
        return ParseResult(
            date=simple.date,
            time=time_str,
            all_day=time_str is None,
            clean_text=clean,
            parser="Fallback",
        )

    time_str = extract_time_heuristic(text)
    clean = remove_time_words(text)
    return ParseResult(
        date=base.isoformat(),
        time=time_str,
        all_day=time_str is None,
        clean_text=clean,
        parser="Fallback",
    )


def parse_task(text: str, base: Optional[date] = None) -> ParseResult:
    base = base or today_local()
    text = cleanup_text(text)
    route = route_task(text)
    logger.info("Router selected %s for text=%r", route, text)

    if route == "rule":
        rule = parse_rule_based(text, base)
        if rule:
            return rule
        # No date signal: keep it usable instead of crashing.
        return ParseResult(
            date=base.isoformat(),
            time=None,
            all_day=True,
            clean_text=text,
            parser="Default",
        )

    try:
        return parse_llm(text, base)
    except Exception:
        logger.exception("LLM parser failed, using fallback for text=%r", text)
        return parse_complex_fallback(text, base)


def parse_date_input(text: str) -> ParseResult:
    """Parse date edits. If user includes time, return it too."""
    return parse_task(text)


def parse_time_input(text: str) -> tuple[Optional[str], bool]:
    lower = cleanup_text(text).lower()
    if lower in {"без времени", "без время", "весь день", "на весь день", "all day", "none", "нет", "-"}:
        return None, True

    time_str = extract_time_heuristic(lower)
    if not time_str:
        raise ValueError(f"Cannot parse time from {text!r}")
    return time_str, False


def parse_task_date(text: str):
    """
    Backward-compatible API for the old bot.py.
    Returns (datetime, clean_text).
    """
    result = parse_task(text)
    dt = datetime.strptime(result.date, "%Y-%m-%d")
    return dt, result.clean_text
