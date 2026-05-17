import logging
from pathlib import Path

import yaml
from aiogram import Bot
from aiogram.fsm.storage.base import BaseStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings

logger = logging.getLogger(__name__)

# Path to the protocols file inside the Obsidian vault.
_PROTOCOLS_PATH = Path(settings.obsidian_vault_path) / "_bot" / "protocols.yaml"

_DEFAULT_PROTOCOLS_YAML = """\
# August AI — Scheduled Protocols
# Cron format: minute hour day month weekday  (standard 5-field cron)
#
# Examples:
#   "0 22 * * *"   — every day at 22:00
#   "0 9 * * 1-5"  — weekdays at 09:00
#   "30 8 * * *"   — every day at 08:30
#
# Fields:
#   id       — unique latin identifier (no spaces)
#   cron     — schedule
#   question — message sent to user
#   file     — log file in _bot/ that must already exist

pings:
  - id: energy
    cron: "0 20 * * *"
    question: "Как твой энергетический уровень сегодня? Оцени 1-10 (можно добавить заметку)."
    file: energy.md

  - id: health
    cron: "0 22 * * *"
    question: "Как самочувствие сегодня? Что было с тренировкой?"
    file: health.md
"""


def _ensure_protocols_file() -> None:
    """Create protocols.yaml with defaults if it doesn't exist yet."""
    if not _PROTOCOLS_PATH.exists():
        _PROTOCOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROTOCOLS_PATH.write_text(_DEFAULT_PROTOCOLS_YAML, encoding="utf-8")
        logger.info("Created default protocols.yaml at %s", _PROTOCOLS_PATH)


def _load_protocols() -> list[dict]:
    """Parse protocols.yaml and return the list of ping definitions."""
    _ensure_protocols_file()
    try:
        with open(_PROTOCOLS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        pings: list[dict] = (data or {}).get("pings", [])
        logger.info("Loaded %d protocol ping(s) from protocols.yaml", len(pings))
        return pings
    except yaml.YAMLError as exc:
        logger.error("protocols.yaml parse error: %s", exc)
        return []
    except OSError as exc:
        logger.error("Cannot read protocols.yaml: %s", exc)
        return []


def _register_protocol_pings(
    scheduler: AsyncIOScheduler,
    bot: Bot,
    storage: BaseStorage,
    pings: list[dict],
) -> None:
    """Register APScheduler jobs for each ping defined in protocols.yaml."""
    from app.jobs.ping_service import send_scheduled_ping

    for entry in pings:
        pid = str(entry.get("id", "")).strip()
        cron = str(entry.get("cron", "")).strip()
        question = str(entry.get("question", "")).strip()
        filename = str(entry.get("file", "")).strip()

        if not all([pid, cron, question, filename]):
            logger.warning("Skipping incomplete ping entry: %s", entry)
            continue

        try:
            trigger = CronTrigger.from_crontab(cron, timezone=settings.task_timezone)
        except Exception as exc:
            logger.error("Invalid cron %r for ping '%s': %s", cron, pid, exc)
            continue

        scheduler.add_job(
            send_scheduled_ping,
            trigger=trigger,
            kwargs={
                "bot": bot,
                "storage": storage,
                "question": question,
                "filename": filename,
            },
            id=f"ping_{pid}",
            replace_existing=True,
        )
        logger.info("Ping '%s' registered: cron='%s' → %s", pid, cron, filename)


def start_scheduler(bot: Bot, storage: BaseStorage) -> AsyncIOScheduler:
    from app.jobs.alert_checker import check_proactive_alerts
    from app.jobs.backlog_nudge import send_backlog_nudge
    from app.jobs.due_pings import check_due_items
    from app.jobs.evening_review import send_evening_review
    from app.jobs.morning_plan import send_morning_plan
    from app.jobs.next_step_push import send_next_step_push
    from app.jobs.recurring_spawn import spawn_recurring_tasks

    scheduler = AsyncIOScheduler(timezone=settings.task_timezone)

    # ── System jobs (hardcoded, always active) ────────────────────────────────
    scheduler.add_job(
        check_due_items,
        trigger=IntervalTrigger(minutes=10),
        args=[bot],
        id="check_due_items",
        replace_existing=True,
    )
    scheduler.add_job(
        send_morning_plan,
        trigger=CronTrigger(hour=8, minute=0, timezone=settings.task_timezone),
        args=[bot],
        id="morning_plan",
        replace_existing=True,
    )
    scheduler.add_job(
        send_next_step_push,
        trigger=CronTrigger(hour=12, minute=30, timezone=settings.task_timezone),
        args=[bot],
        id="next_step_push",
        replace_existing=True,
    )
    scheduler.add_job(
        check_proactive_alerts,
        trigger=CronTrigger(hour=20, minute=30, timezone=settings.task_timezone),
        args=[bot],
        id="proactive_alerts",
        replace_existing=True,
    )
    scheduler.add_job(
        send_evening_review,
        trigger=CronTrigger(hour=21, minute=0, timezone=settings.task_timezone),
        args=[bot],
        id="evening_review",
        replace_existing=True,
    )

    scheduler.add_job(
        send_backlog_nudge,
        trigger=CronTrigger(hour=14, minute=0, timezone=settings.task_timezone),
        args=[bot],
        id="backlog_nudge",
        replace_existing=True,
    )
    scheduler.add_job(
        spawn_recurring_tasks,
        trigger=CronTrigger(hour=7, minute=0, timezone=settings.task_timezone),
        args=[bot],
        id="recurring_spawn",
        replace_existing=True,
    )

    # ── Protocol pings (from _bot/protocols.yaml) ─────────────────────────────
    pings = _load_protocols()
    _register_protocol_pings(scheduler, bot, storage, pings)

    scheduler.start()
    logger.info(
        "Scheduler started (tz=%s): system jobs + %d protocol ping(s)",
        settings.task_timezone,
        len(pings),
    )
    return scheduler
