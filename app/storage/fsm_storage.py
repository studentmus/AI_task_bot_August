"""SQLite-backed FSM storage for aiogram 3.

Replaces the default MemoryStorage so FSM states (LogState, PingState,
EditStates) survive bot restarts. Data is stored in data/fsm.db alongside
tasks.db.

Key format: "{bot_id}:{chat_id}:{user_id}:{destiny}"
"""

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType


logger = logging.getLogger(__name__)


class SQLiteFSMStorage(BaseStorage):
    """Persistent FSM storage backed by SQLite."""

    def __init__(self, path: str) -> None:
        db_path = Path(path).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(db_path)
        self._lock: asyncio.Lock | None = None  # lazily created inside event loop
        self._init_db()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fsm_state (
                    key   TEXT PRIMARY KEY,
                    state TEXT,
                    data  TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.commit()
        logger.info("SQLiteFSMStorage ready at %s", self._path)

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @staticmethod
    def _key(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.destiny}"

    # ── Sync DB helpers (run via asyncio.to_thread) ───────────────────────────

    def _sync_set_state(self, k: str, state_str: Optional[str]) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """INSERT INTO fsm_state (key, state) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET state = excluded.state""",
                (k, state_str),
            )
            conn.commit()

    def _sync_get_state(self, k: str) -> Optional[str]:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT state FROM fsm_state WHERE key = ?", (k,)
            ).fetchone()
        return row[0] if row else None

    def _sync_set_data(self, k: str, data_str: str) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """INSERT INTO fsm_state (key, data) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET data = excluded.data""",
                (k, data_str),
            )
            conn.commit()

    def _sync_get_data(self, k: str) -> dict[str, Any]:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT data FROM fsm_state WHERE key = ?", (k,)
            ).fetchone()
        if not row or not row[0]:
            return {}
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return {}

    # ── BaseStorage interface ─────────────────────────────────────────────────

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        k = self._key(key)
        # State can be a State object or str; convert to str
        if hasattr(state, "state"):
            state_str: Optional[str] = state.state  # type: ignore[union-attr]
        else:
            state_str = state  # type: ignore[assignment]
        async with self._get_lock():
            await asyncio.to_thread(self._sync_set_state, k, state_str)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        async with self._get_lock():
            return await asyncio.to_thread(self._sync_get_state, self._key(key))

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        k = self._key(key)
        data_str = json.dumps(data, ensure_ascii=False)
        async with self._get_lock():
            await asyncio.to_thread(self._sync_set_data, k, data_str)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._get_lock():
            return await asyncio.to_thread(self._sync_get_data, self._key(key))

    async def close(self) -> None:
        pass  # connections opened per-operation, nothing to close
