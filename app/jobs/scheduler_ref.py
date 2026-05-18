"""Global scheduler reference — set once at startup, read by dynamic job creators."""
from typing import Optional

_scheduler = None


def set_scheduler(s) -> None:
    global _scheduler
    _scheduler = s


def get_scheduler():
    return _scheduler
