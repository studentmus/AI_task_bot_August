"""Alert throttling: prevents sending the same alert every day.

Stores last-sent timestamps in data/alert_throttle.json.
Survives restarts (file-backed), no DB migration needed.
"""
import json
import logging
from datetime import datetime
from pathlib import Path

from app.domain.alert_rules import Alert

logger = logging.getLogger(__name__)

_THROTTLE_FILE = Path("data/alert_throttle.json")

# Per-key cooldown in days. Persistent issues get longer windows to avoid spam.
_COOLDOWNS: dict[str, int] = {
    "protein_low_2d":           2,
    "nutrition_no_log_2d":      2,
    "training_skip_3d":         2,
    "training_zero_this_week":  3,
    "sleep_short_2n":           2,
    "sleep_late_bed":           2,
    "german_gap_3d":            2,
    "romanian_gap_5d":          3,
    # Info alerts — shorter cooldown
    "protein_low_1d":           1,
    "training_skip_2d":         1,
    "sleep_short_1n":           1,
    "german_gap_2d":            1,
    "romanian_gap_3d":          2,
}
_DEFAULT_COOLDOWN = 1  # days


def _load() -> dict[str, str]:
    if not _THROTTLE_FILE.exists():
        return {}
    try:
        return json.loads(_THROTTLE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("alert_throttle load failed: %s", exc)
        return {}


def _save(data: dict[str, str]) -> None:
    try:
        _THROTTLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _THROTTLE_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("alert_throttle save failed: %s", exc)


def filter_alerts(alerts: list[Alert]) -> list[Alert]:
    """Remove alerts whose cooldown period has not yet expired."""
    data = _load()
    now = datetime.now()
    result: list[Alert] = []
    for alert in alerts:
        last_str = data.get(alert.key)
        if last_str:
            try:
                last = datetime.fromisoformat(last_str)
                cooldown = _COOLDOWNS.get(alert.key, _DEFAULT_COOLDOWN)
                if (now - last).days < cooldown:
                    logger.debug("Alert %r throttled (%d days cooldown)", alert.key, cooldown)
                    continue
            except ValueError:
                pass
        result.append(alert)
    return result


def mark_alerts_sent(alerts: list[Alert]) -> None:
    """Record current timestamp for each alert so future calls can throttle them."""
    if not alerts:
        return
    data = _load()
    now = datetime.now().isoformat(timespec="seconds")
    for alert in alerts:
        data[alert.key] = now
    _save(data)
