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


# ---------------------------------------------------------------------------
# Стоп-фразы: служебные слова, которые пользователь адресует боту,
# а не включает в название задачи.
# Порядок важен: более длинные (специфичные) варианты — раньше коротких,
# иначе "создай" будет срабатывать раньше "создай задачу".
# ---------------------------------------------------------------------------
STOP_PHRASES: tuple[str, ...] = (
    # команды создания / планирования
    "создай задачу",       "создай напоминание",      "создай",
    "добавь задачу",       "добавь напоминание",       "добавь в список",
    "поставь задачу",      "поставь напоминание",      "поставь",
    "запланируй",          "запланировать",
    "внеси задачу",        "внеси",
    # напоминания
    "напомни мне",         "напомни",
    "напомнить мне",       "напомнить",
    # вводные маркеры намерения
    "нужно не забыть",     "не забыть",                "не забудь",
    "хочу не забыть",
    "нужно мне",           "нужно",
    "надо мне",            "надо",
    "хочу мне",            "хочу",
    # маркер «задача» как артикль
    "задача:",             "задачу",                   "задача",
)


def cleanup_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text, flags=re.UNICODE).strip(" ,.!?;:-\n\t")
    return text or "Без названия"


# ---------------------------------------------------------------------------
# Парсинг временны́х диапазонов «с X до Y [суффикс]»
# ---------------------------------------------------------------------------

# Суффиксы, определяющие AM/PM в разговорном русском
_AMPM_SUFFIX = r"(?:утра|дня|вечера|ночи|am|pm)"

# Паттерн диапазона: «с X[:MM] [суф?] до Y[:MM] [суф?]»
# Группы: 1=start_h, 2=start_m, sfx1=start_sfx, 3=end_h, 4=end_m, sfx2=end_sfx
_TIME_RANGE_FULL_RE = re.compile(
    r"\bс\s+"
    r"(\d{1,2})(?:[:](\d{2}))?"
    r"\s*(?P<sfx1>" + _AMPM_SUFFIX + r")?"
    r"\s+до\s+"
    r"(\d{1,2})(?:[:](\d{2}))?"
    r"\s*(?P<sfx2>" + _AMPM_SUFFIX + r")?",
    re.IGNORECASE,
)


def _adjust_hour(h: int, suffix: str | None) -> int:
    """Переводит час в 24-часовой формат на основе разговорного суффикса."""
    if not suffix:
        return h
    s = suffix.lower()
    if s in ("дня", "вечера", "pm") and h < 12:
        return h + 12
    if s in ("утра", "am") and h == 12:
        return 0
    # "ночи": 1-5 ночи = ранее утро (01-05), 11 ночи = 23 — оставляем как есть
    return h


def _extract_time_range_natural(text: str) -> str | None:
    """Извлекает диапазон HH:MM-HH:MM из фраз типа 'с 10 до 4 дня', 'с 9 утра до 5 вечера'.
    Возвращает строку 'HH:MM-HH:MM' или None если не нашёл."""
    m = _TIME_RANGE_FULL_RE.search(text.lower())
    if not m:
        return None
    sh = _adjust_hour(int(m.group(1)), m.group("sfx1"))
    sm = int(m.group(2)) if m.group(2) else 0
    eh = _adjust_hour(int(m.group(3)), m.group("sfx2"))
    em = int(m.group(4)) if m.group(4) else 0
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
        return None
    return f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"


def strip_stop_phrases(text: str) -> str:
    """Итеративно удаляет стоп-фразы из начала названия задачи.

    Пример: 'создай задачу нужно встреча' → 'встреча'
    Если после стрипа остаётся пустая строка — возвращает исходный текст.
    """
    result = text
    changed = True
    while changed:
        changed = False
        lower_r = result.lower()
        for phrase in STOP_PHRASES:
            if lower_r.startswith(phrase):
                result = result[len(phrase):].lstrip(" ,:;-\t")
                changed = True
                break
    cleaned = cleanup_text(result)
    # Защита: если всё оказалось стоп-словами — не затираем текст в «Без названия»
    return cleaned if cleaned and cleaned != "Без названия" else text


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
        r"\b\d{1,2}[:.]\d{2}\b",                            # 15:00  15.00
        r"\b(?:в|к|на)\s+\d{1,2}\b",                        # в 15  на 15  к 3
        r"\b(?:в|к)\s*\d{1,2}\s*(?:час|часа|часов|ч)\b",    # в 3 часа
        r"\b\d{1,2}\s*(?:час|часа|часов)\b",                 # 3 часа
        r"\b\d{1,2}\s*(?:am|pm)\b",                          # 3pm
        r"\bс\s+\d{1,2}(?:[:.]\d{2})?\s+до\s+\d{1,2}",     # с 10 до 12
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


def parse_rule_based(text: str, base: Optional[date] = None) -> list[ParseResult]:
    base = base or today_local()
    lower = text.lower()

    for pattern, delta in [
        (r"\bсегодня\b", 0),
        (r"\bзавтра\b", 1),
        (r"\bпослезавтра\b", 2),
    ]:
        m = re.search(pattern, lower)
        if m:
            return [ParseResult(
                date=(base + timedelta(days=delta)).isoformat(),
                time=None,
                all_day=True,
                clean_text=remove_span(text, m.start(), m.end()),
                parser="Rule-Based",
            )]

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
            return []
        if not m.group(3) and target < base:
            target = date(base.year + 1, month_num, day_num)
        return [ParseResult(
            date=target.isoformat(),
            time=None,
            all_day=True,
            clean_text=remove_span(text, m.start(), m.end()),
            parser="Rule-Based",
        )]

    m = re.search(r"\b(?:на\s+)?(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", lower)
    if m:
        day_num, month_num = int(m.group(1)), int(m.group(2))
        year_raw = m.group(3)
        year_num = int(year_raw) + (2000 if year_raw and int(year_raw) < 100 else 0) if year_raw else base.year
        try:
            target = date(year_num, month_num, day_num)
        except ValueError:
            return []
        if not year_raw and target < base:
            target = date(base.year + 1, month_num, day_num)
        return [ParseResult(
            date=target.isoformat(),
            time=None,
            all_day=True,
            clean_text=remove_span(text, m.start(), m.end()),
            parser="Rule-Based",
        )]

    # "в следующий/следующую/следующее понедельник/пятницу/..."
    weekday_names = "|".join(sorted(WEEKDAYS.keys(), key=len, reverse=True))
    m = re.search(
        rf"\b(?:в|во|на)\s+(?:следующ\w*\s+)?({weekday_names})\b",
        lower,
        flags=re.IGNORECASE,
    )
    if m:
        return [ParseResult(
            date=next_weekday(base, WEEKDAYS[m.group(1).lower()]).isoformat(),
            time=None,
            all_day=True,
            clean_text=remove_span(text, m.start(), m.end()),
            parser="Rule-Based",
        )]

    return []


def _pm_heuristic(h: int, lower: str) -> int:
    """1-6 без явного утра/ам → скорее вечер (PM)."""
    if 1 <= h <= 6 and not any(w in lower for w in ("утра", "ноч", "am")):
        return h + 12
    return h


def extract_time_heuristic(text: str) -> Optional[str]:
    lower = text.lower()

    # 1. Диапазон «с X до Y»
    range_result = _extract_time_range_natural(lower)
    if range_result:
        return range_result

    # 2. Словесные времена суток
    for word, t in TIME_WORDS.items():
        if word in lower:
            return t

    # 3. HH:MM или HH.MM
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", lower)
    if m:
        return validate_time(f"{int(m.group(1)):02d}:{int(m.group(2)):02d}")

    # 4. "HH MM" через пробел — "15 30" → 15:30
    m = re.search(r"(?<!\d)(\d{1,2})\s+(\d{2})(?!\d)", lower)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return f"{h:02d}:{mn:02d}"

    # 5. Предлог + голое число: "в 15", "к 3", "на 15"
    m = re.search(r"\b(?:в|к|на)\s+([01]?\d|2[0-3])\b", lower)
    if m:
        h = _pm_heuristic(int(m.group(1)), lower)
        if 0 <= h <= 23:
            return f"{h:02d}:00"

    # 6. N часов/часа/ч  (с необязательным "в/к")
    m = re.search(r"\b(?:(?:в|к)\s*)?([01]?\d|2[0-3])\s*(?:час|часа|часов|ч)\b", lower)
    if m:
        h = _pm_heuristic(int(m.group(1)), lower)
        if 0 <= h <= 23:
            return f"{h:02d}:00"

    return None


def remove_time_words(text: str) -> str:
    patterns = [
        # Диапазон «с X до Y» — первым
        r"\bс\s+\d{1,2}(?:[:.]\d{2})?\s*(?:утра|дня|вечера|ночи|am|pm)?"
        r"\s+до\s+\d{1,2}(?:[:.]\d{2})?\s*(?:утра|дня|вечера|ночи|am|pm)?\b",
        r"\b(?:в|к|на)\s+\d{1,2}[:.]\d{2}\b",    # в/к/на 15:30
        r"\b\d{1,2}[:.]\d{2}\b",                   # 15:30
        r"\b(?:в|к|на)\s+\d{1,2}\b",               # в 15, на 3, к 9
        r"\b\d{1,2}\s+\d{2}\b",                    # 15 30 (пробел вместо двоеточия)
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
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {value!r}")


def _validate_single_time(value: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not m:
        raise ValueError(f"Invalid time: {value!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time: {value!r}")
    return f"{hour:02d}:{minute:02d}"


def validate_time(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    if "-" in value:
        start_raw, end_raw = value.split("-", 1)
        return f"{_validate_single_time(start_raw)}-{_validate_single_time(end_raw)}"
    return _validate_single_time(value)


def fix_ambiguous_short_hour(original_text: str, time_value: Optional[str]) -> Optional[str]:
    """Make 'в 3 часа' → 15:00, not 03:00."""
    if not time_value or "-" in time_value:
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


def parse_llm(text: str, base: Optional[date] = None, context: Optional[str] = None) -> list[ParseResult]:
    from app.llm.deepseek_client import call_deepseek_parse
    from app.llm.prompt_builder import build_task_prompt

    base = base or today_local()
    prompt = build_task_prompt(text, base, context=context)
    parsed = call_deepseek_parse(prompt)

    # Новый формат: {"tasks": [...]}; старый формат (один объект) — обратная совместимость.
    raw_tasks = parsed.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raw_tasks = [parsed]

    results: list[ParseResult] = []
    for item in raw_tasks:
        try:
            date_str = validate_date(item["date"])
            time_str = validate_time(item.get("time"))
            time_str = fix_ambiguous_short_hour(text, time_str)
            all_day = not bool(time_str)
            clean = _sanitize_llm_clean_text(str(item.get("clean_text") or ""), text)
            results.append(ParseResult(
                date=date_str,
                time=time_str,
                all_day=all_day,
                clean_text=clean,
                parser="DeepSeek",
            ))
        except Exception as exc:
            logger.warning("Skipping invalid task item %s: %s", item, exc)

    if not results:
        raise ValueError("LLM вернул пустой или невалидный список задач")

    return results


def parse_complex_fallback(text: str, base: Optional[date] = None) -> list[ParseResult]:
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
        return [ParseResult(
            date=target.isoformat(),
            time=time_str,
            all_day=time_str is None,
            clean_text=remove_time_words(remove_span(text, m.start(), m.end())),
            parser="Fallback",
        )]

    simple_list = parse_rule_based(text, base)
    if simple_list:
        simple = simple_list[0]
        time_str = extract_time_heuristic(text)
        return [ParseResult(
            date=simple.date,
            time=time_str,
            all_day=time_str is None,
            clean_text=remove_time_words(simple.clean_text),
            parser="Fallback",
        )]

    time_str = extract_time_heuristic(text)
    return [ParseResult(
        date=base.isoformat(),
        time=time_str,
        all_day=time_str is None,
        clean_text=remove_time_words(text),
        parser="Fallback",
    )]


# Сигналы сложного / составного текста — такие сообщения сразу идут в LLM для разбивки.
_COMPLEX_SIGNALS_RE = re.compile(r",| и | а | потом | затем ", re.IGNORECASE)


def _is_complex_text(text: str) -> bool:
    return bool(_COMPLEX_SIGNALS_RE.search(text)) or len(text) > 40


def _apply_stop_phrases(results: list[ParseResult]) -> list[ParseResult]:
    """Прогоняет clean_text каждого результата через strip_stop_phrases."""
    for r in results:
        r.clean_text = strip_stop_phrases(r.clean_text)
    return results


def parse_task(text: str, base: Optional[date] = None, context: Optional[str] = None) -> list[ParseResult]:
    base = base or today_local()
    text = cleanup_text(text)

    # Составной или длинный текст → сразу LLM, чтобы разбить на несколько задач.
    if _is_complex_text(text):
        logger.info("Complex text → LLM for %r", text)
        try:
            return _apply_stop_phrases(parse_llm(text, base, context=context))
        except Exception:
            logger.exception("LLM failed, using fallback for %r", text)
            return _apply_stop_phrases(parse_complex_fallback(text, base))

    route = route_task(text)
    logger.info("Router: %s for %r", route, text)

    if route == "rule":
        results = parse_rule_based(text, base)
        if results:
            return _apply_stop_phrases(results)
        raise ValueError(
            "Не нашёл дату в тексте. Попробуй: завтра, 4 мая, в пятницу, через 3 дня."
        )

    try:
        return _apply_stop_phrases(parse_llm(text, base, context=context))
    except Exception:
        logger.exception("LLM failed, using fallback for %r", text)
        return _apply_stop_phrases(parse_complex_fallback(text, base))


def parse_date_input(text: str) -> ParseResult:
    return parse_task(text)[0]


def parse_time_input(text: str) -> tuple[Optional[str], bool]:
    lower = cleanup_text(text).lower().strip()

    # Отмена времени
    if lower in {"без времени", "без время", "весь день", "на весь день", "all day", "none", "нет", "-"}:
        return None, True

    # Явный диапазон "HH:MM-HH:MM"
    m = re.fullmatch(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", lower)
    if m:
        return f"{_validate_single_time(m.group(1))}-{_validate_single_time(m.group(2))}", False

    # Диапазон без минут "H-H", смешанный "H:MM-H", "H-H:MM"
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?", lower)
    if m:
        sh, sm_ = int(m.group(1)), int(m.group(2) or 0)
        eh, em_ = int(m.group(3)), int(m.group(4) or 0)
        if 0 <= sh <= 23 and 0 <= sm_ <= 59 and 0 <= eh <= 23 and 0 <= em_ <= 59:
            return f"{sh:02d}:{sm_:02d}-{eh:02d}:{em_:02d}", False

    # Диапазон на естественном языке: "с 10 до 12", "с 9 утра до 5 вечера"
    range_result = _extract_time_range_natural(lower)
    if range_result:
        start_str, end_str = range_result.split("-")
        return f"{_validate_single_time(start_str)}-{_validate_single_time(end_str)}", False

    # Голое число: "15" → 15:00  "3" → 15:00 (PM heuristic)
    m = re.fullmatch(r"(\d{1,2})", lower)
    if m:
        h = _pm_heuristic(int(m.group(1)), lower)
        if 0 <= h <= 23:
            return f"{h:02d}:00", False

    # Пробел вместо двоеточия: "15 30" → 15:30  "9 00" → 09:00
    m = re.fullmatch(r"(\d{1,2})\s+(\d{2})", lower)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return f"{h:02d}:{mn:02d}", False

    # Остальное через универсальный heuristic
    time_str = extract_time_heuristic(lower)
    if not time_str:
        raise ValueError(f"Cannot parse time from {text!r}")
    return time_str, False
