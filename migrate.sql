-- AI Task Manager v4 migration.
-- Run once:
--   sqlite3 /data/data/com.termux/files/home/ai-stack/bot/tasks.db < migrate.sql
--
-- If you already started the new bot.py, these columns may already exist.
-- In that case SQLite can print "duplicate column name"; it is safe to ignore.

BEGIN;

ALTER TABLE tasks ADD COLUMN event_time TEXT;
ALTER TABLE tasks ADD COLUMN all_day BOOLEAN DEFAULT 1;

UPDATE tasks
SET all_day = 1
WHERE all_day IS NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_status_radicale
ON tasks(status, radicale_uid);

COMMIT;
