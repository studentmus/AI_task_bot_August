"""Structured data extraction from free-text log entries.

Nutrition and training: async LLM call (background, non-blocking).
Sleep: sync regex parse from already-formatted text (no LLM needed).
"""
import asyncio
import logging
import re

logger = logging.getLogger(__name__)

# ── Sleep (no LLM) ────────────────────────────────────────────────────────────
# Matches "23:30–07:00 (7ч 30м)" produced by parse_sleep_time in log_handler.
_SLEEP_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})[–\-](\d{1,2}):(\d{2})")
_SLEEP_DUR_RE   = re.compile(r"\((\d+)ч(?:[.\s]*(\d+)м)?\)")


def _extract_sleep_sync(text: str) -> dict | None:
    r = _SLEEP_RANGE_RE.search(text)
    d = _SLEEP_DUR_RE.search(text)
    if not r and not d:
        return None
    result: dict = {}
    if r:
        result["bedtime"]  = f"{int(r.group(1)):02d}:{int(r.group(2)):02d}"
        result["wakeup"]   = f"{int(r.group(3)):02d}:{int(r.group(4)):02d}"
    if d:
        result["duration_min"] = int(d.group(1)) * 60 + int(d.group(2) or 0)
    return result or None


# ── LLM prompts ───────────────────────────────────────────────────────────────

_NUTRITION_PROMPT = """\
Ты помощник-диетолог. Из записи о питании извлеки макронутриенты.
Используй стандартные значения для указанных продуктов и порций (русская/европейская кухня).

Запись: "{text}"

Верни JSON строго в формате:
{{"protein_g": <число или null>, "calories": <число или null>, "fat_g": <число или null>, "carbs_g": <число или null>}}

Правила:
- Округляй до целых.
- Если продукт/порция неизвестны — null для этого поля.
- Не добавляй никаких других полей.\
"""

_TRAINING_PROMPT = """\
Извлеки структурированные данные из записи о тренировке.

Запись: "{text}"

Верни JSON строго в формате:
{{"session_type": "strength" | "cardio" | "mobility" | "other",
  "exercises": [{{"name": "<название на английском>", "sets": <int или null>, "reps": <int или null>, "weight_kg": <float или null>}}],
  "duration_min": <int или null>,
  "total_volume_kg": <int или null>}}

Правила:
- total_volume_kg = сумма (weight_kg * sets * reps) для силовых упражнений, иначе null.
- Если упражнений нет — exercises: [].
- Не добавляй другие поля.\
"""


# ── Async LLM extraction ──────────────────────────────────────────────────────

async def _call_parse(prompt: str) -> dict | None:
    from app.llm.deepseek_client import call_deepseek_parse
    try:
        return await asyncio.to_thread(call_deepseek_parse, prompt)
    except Exception as exc:
        logger.warning("log_parser LLM call failed: %s", exc)
        return None


async def _extract_nutrition(text: str) -> dict | None:
    return await _call_parse(_NUTRITION_PROMPT.format(text=text))


async def _extract_training(text: str) -> dict | None:
    data = await _call_parse(_TRAINING_PROMPT.format(text=text))
    if data is None:
        return None
    # Compute total_volume_kg if LLM left it null but exercises are present
    if data.get("total_volume_kg") is None:
        vol = 0
        for ex in data.get("exercises") or []:
            w = ex.get("weight_kg") or 0
            s = ex.get("sets") or 0
            r = ex.get("reps") or 0
            vol += w * s * r
        if vol > 0:
            data["total_volume_kg"] = int(vol)
    return data


# ── Energy (no LLM) ──────────────────────────────────────────────────────────
# Matches "7", "7/10", "7 из 10" anywhere in text

_ENERGY_RE = re.compile(r"\b(10|[1-9])(?:\s*(?:/|из)\s*10)?\b")


def _extract_energy_sync(text: str) -> dict | None:
    m = _ENERGY_RE.search(text)
    if not m:
        return None
    level = int(m.group(1))
    # Remove the matched fragment and keep the rest as notes
    notes = _ENERGY_RE.sub("", text, count=1)
    notes = re.sub(r"[,;./\-–—]\s*$", "", notes.strip()).strip()
    notes = re.sub(r"^\s*[,;./\-–—]", "", notes).strip()
    return {"energy": level, "notes": notes or None}


# ── Public entry point ────────────────────────────────────────────────────────

async def extract_structured(sphere: str, text: str) -> dict | None:
    """Return a structured dict for the given sphere/text, or None if not applicable."""
    if sphere == "sleep":
        return _extract_sleep_sync(text)
    if sphere == "nutrition":
        return await _extract_nutrition(text)
    if sphere == "training":
        return await _extract_training(text)
    if sphere == "energy":
        return _extract_energy_sync(text)
    return None
