import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiofiles

from app.config import settings

logger = logging.getLogger(__name__)

# Сериализует параллельные git-операции: если пользователь пишет несколько
# записей подряд, они встают в очередь и не конфликтуют друг с другом.
_GIT_LOCK = asyncio.Lock()

# ── In-process кэш bot_memory.md ─────────────────────────────────────────────
# Читается синхронно (локальный файл, мгновенно) и кэшируется на 5 минут.
# Инвалидируется сразу при сохранении нового факта через save_fact_to_obsidian.
_MEMORY_CACHE: dict = {"content": "", "ts": 0.0}
_MEMORY_TTL = 300  # секунд


def read_memory_sync() -> str:
    """Синхронное чтение bot_memory.md с TTL-кэшем. Подходит для вызова из build_messages."""
    now = time.monotonic()
    if now - _MEMORY_CACHE["ts"] < _MEMORY_TTL:
        return _MEMORY_CACHE["content"]
    path = Path(settings.obsidian_vault_path) / "_bot" / "bot_memory.md"
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        content = ""
    _MEMORY_CACHE["content"] = content
    _MEMORY_CACHE["ts"] = now
    return content


def invalidate_memory_cache() -> None:
    """Сбрасывает кэш — вызывается после записи нового факта."""
    _MEMORY_CACHE["ts"] = 0.0


# ── In-process кэш energy_matrix.md ──────────────────────────────────────────
_ENERGY_MATRIX_CACHE: dict = {"content": "", "ts": 0.0}
_ENERGY_MATRIX_TTL = 300  # секунд


def read_energy_matrix_sync() -> str:
    """Синхронное чтение energy_matrix.md с TTL-кэшем 5 мин."""
    now = time.monotonic()
    if now - _ENERGY_MATRIX_CACHE["ts"] < _ENERGY_MATRIX_TTL:
        return _ENERGY_MATRIX_CACHE["content"]
    path = Path(settings.obsidian_vault_path) / "_bot" / "energy_matrix.md"
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        content = ""
    _ENERGY_MATRIX_CACHE["content"] = content
    _ENERGY_MATRIX_CACHE["ts"] = now
    return content


# ── In-process кэш project_*.md файлов ───────────────────────────────────────
_PROJECTS_CACHE: dict = {"content": "", "ts": 0.0}
_PROJECTS_TTL = 300  # секунд

_PROJECT_SECTIONS = {"## Current State", "## Next Actions"}
_MAX_SECTION_CHARS = 400  # максимум символов на секцию


def _extract_project_sections(text: str) -> str:
    """Извлекает только ## Current State и ## Next Actions из файла проекта."""
    lines = text.splitlines()
    result: list[str] = []
    in_section = False
    char_count = 0

    for line in lines:
        if line.startswith("## "):
            in_section = any(line.startswith(s) for s in _PROJECT_SECTIONS)
            if in_section:
                result.append(line)
            continue
        if in_section:
            if char_count < _MAX_SECTION_CHARS:
                result.append(line)
                char_count += len(line)
            elif not result[-1].endswith("…"):
                result.append("…")

    return "\n".join(result).strip()


def read_project_files_sync() -> str:
    """Читает все _bot/project_*.md файлы, возвращает Current State + Next Actions. TTL 5 мин."""
    now = time.monotonic()
    if now - _PROJECTS_CACHE["ts"] < _PROJECTS_TTL:
        return _PROJECTS_CACHE["content"]

    bot_dir = Path(settings.obsidian_vault_path) / "_bot"
    parts: list[str] = []

    for path in sorted(bot_dir.glob("project_*.md")):
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except (OSError, FileNotFoundError):
            continue
        extracted = _extract_project_sections(raw)
        if extracted:
            parts.append(f"=== {path.stem} ===\n{extracted}")

    content = "\n\n".join(parts)
    _PROJECTS_CACHE["content"] = content
    _PROJECTS_CACHE["ts"] = now
    return content


def _write_log_entry(sphere: str, raw_text: str, logged_at: str) -> int | None:
    """Дублирует запись лога в SQLite. Возвращает entry ID или None при ошибке."""
    user_id = settings.allowed_user_id
    if not user_id:
        return None
    try:
        from app.storage.db import SessionLocal
        from app.storage.log_repo import LogRepo
        with SessionLocal() as session:
            return LogRepo(session).insert(
                user_id=user_id,
                sphere=sphere,
                raw_text=raw_text,
                logged_at=logged_at,
            )
    except Exception as exc:
        logger.warning("log_entry SQLite write failed: %s", exc)
        return None


def _schedule_extraction(entry_id: int, sphere: str, raw_text: str) -> None:
    """Запускает LLM-парсинг структурированных данных в фоне. Обновляет запись по ID."""
    async def _task() -> None:
        try:
            from app.domain.log_parser import extract_structured
            data = await extract_structured(sphere, raw_text)
            if data:
                from app.storage.db import SessionLocal
                from app.storage.log_repo import LogRepo
                with SessionLocal() as session:
                    LogRepo(session).update_structured_data(entry_id, data)
                logger.debug("Structured data saved for entry id=%s: %s", entry_id, data)
        except Exception as exc:
            logger.warning("Extraction failed for entry id=%s: %s", entry_id, exc)

    asyncio.create_task(_task())

_LOG_HEADER = "## Log"

# Маппинг всех русских синонимов и английских имён → канонические имена файлов
SPHERE_MAP: dict[str, str] = {
    "питание":   "nutrition",
    "еда":       "nutrition",
    "nutrition": "nutrition",
    "сон":       "sleep",
    "sleep":     "sleep",
    "тренировки": "training",
    "тренировка": "training",
    "спорт":     "training",
    "зал":       "training",
    "training":  "training",
    "немецкий":  "german",
    "дойч":      "german",
    "german":    "german",
    "румынский": "romanian",
    "romanian":  "romanian",
    "контекст":  "ivan_context",
    "задачи":    "tasks",
    "tasks":     "tasks",
    "энергия":   "energy",
    "energy":    "energy",
    "wishlist":  "wishlist",
    "список":    "wishlist",
    "покупки":   "wishlist",
    "wish":      "wishlist",
}

# Уникальные канонические имена — показываем в сообщениях об ошибке
KNOWN_SPHERES = ", ".join(sorted(set(SPHERE_MAP.values())))


def _resolve_sphere(sphere: str) -> str:
    """Приводит любой синоним к каноническому имени файла."""
    return SPHERE_MAP.get(sphere.strip().lower(), sphere.strip().lower())


def _sphere_path(sphere: str) -> Path:
    return Path(settings.obsidian_vault_path) / "_bot" / f"{_resolve_sphere(sphere)}.md"


async def read_obsidian_protocol(sphere: str) -> str:
    path = _sphere_path(sphere)
    try:
        async with aiofiles.open(path, encoding="utf-8") as f:
            return await f.read()
    except FileNotFoundError:
        return f"Файл {path} не найден."
    except OSError as exc:
        logger.error("read_obsidian_protocol: %s", exc)
        return f"Ошибка чтения файла: {exc}"


async def append_obsidian_log(sphere: str, entry: str) -> str:
    resolved = _resolve_sphere(sphere)
    path = _sphere_path(sphere)

    # Защита от создания неизвестных файлов: только существующие протоколы
    if not path.exists():
        return (
            f"Ошибка: Протокол для сферы '{sphere}' не существует. "
            f"Доступные протоколы в базе знаний: {KNOWN_SPHERES}. "
            f"Спроси пользователя, хочет ли он создать новый протокол "
            f"или записать в существующий."
        )

    tz = ZoneInfo(settings.task_timezone)
    timestamp = datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M")
    log_line = f"- [{timestamp}] {entry}\n"

    try:
        async with aiofiles.open(path, encoding="utf-8") as f:
            content = await f.read()

        if _LOG_HEADER in content:
            lines = content.splitlines(keepends=True)
            insert_at = next(
                (i + 1 for i, ln in enumerate(lines) if ln.strip() == _LOG_HEADER),
                None,
            )
            if insert_at is not None:
                lines.insert(insert_at, log_line)
                new_content = "".join(lines)
            else:
                new_content = content + log_line
        else:
            sep = "\n" if content and not content.endswith("\n") else ""
            new_content = f"{content}{sep}\n{_LOG_HEADER}\n{log_line}"

        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(new_content)

    except OSError as exc:
        logger.error("append_obsidian_log write: %s", exc)
        return f"Ошибка записи файла: {exc}"

    entry_id = _write_log_entry(sphere=resolved, raw_text=entry, logged_at=timestamp)
    if entry_id is not None:
        _schedule_extraction(entry_id, resolved, entry)
    else:
        logger.warning("log_entry NOT written to SQLite for sphere=%s — check allowed_user_id config", resolved)
    _schedule_git_sync(f"_bot/{resolved}.md", resolved)
    return "Запись добавлена."


_MEMORY_FILE = "_bot/bot_memory.md"

# Matches a memory file line: "- [YYYY-MM-DD] Факт: <content>"
_FACT_LINE_RE = re.compile(r"^- \[\d{4}-\d{2}-\d{2}\] Факт: (.+)$")

_STOP_WORDS = frozenset({
    "в", "на", "с", "по", "из", "у", "к", "за", "от", "до", "при", "для",
    "что", "как", "это", "мой", "его", "её", "их", "нет", "не", "и", "или",
    "но", "а", "же", "бы", "ли", "со", "об", "под", "над",
})

_DEDUP_THRESHOLD = 0.60


def _fact_tokens(text: str) -> frozenset[str]:
    """Significant lowercased tokens from a fact string (len ≥ 3, not stop-word)."""
    raw = re.findall(r"[а-яёa-z\d]+", text.lower())
    return frozenset(t for t in raw if len(t) >= 3 and t not in _STOP_WORDS)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _find_duplicate_idx(lines: list[str], new_fact: str) -> int | None:
    """Return index of the most similar existing fact line, or None if below threshold."""
    new_tokens = _fact_tokens(new_fact)
    if not new_tokens:
        return None

    best_idx: int | None = None
    best_score = 0.0

    for i, line in enumerate(lines):
        m = _FACT_LINE_RE.match(line.rstrip())
        if not m:
            continue
        score = _jaccard(new_tokens, _fact_tokens(m.group(1)))
        if score > best_score:
            best_score = score
            best_idx = i

    return best_idx if best_score >= _DEDUP_THRESHOLD else None


def _memory_path() -> Path:
    return Path(settings.obsidian_vault_path) / "_bot" / "bot_memory.md"


async def save_fact_to_obsidian(fact: str) -> str:
    path = _memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    new_line = f"- [{date_str}] Факт: {fact}"

    try:
        replaced_text: str | None = None

        if path.exists():
            async with aiofiles.open(path, encoding="utf-8") as f:
                content = await f.read()

            lines = content.splitlines()
            dup_idx = _find_duplicate_idx(lines, fact)

            if dup_idx is not None:
                m = _FACT_LINE_RE.match(lines[dup_idx].rstrip())
                replaced_text = m.group(1) if m else lines[dup_idx]
                lines[dup_idx] = new_line
                new_content = "\n".join(lines)
                if not new_content.endswith("\n"):
                    new_content += "\n"
                async with aiofiles.open(path, "w", encoding="utf-8") as f:
                    await f.write(new_content)
            else:
                async with aiofiles.open(path, "a", encoding="utf-8") as f:
                    await f.write(new_line + "\n")
        else:
            async with aiofiles.open(path, "a", encoding="utf-8") as f:
                await f.write(new_line + "\n")

    except OSError as exc:
        logger.error("save_fact_to_obsidian write: %s", exc)
        return f"Ошибка записи файла памяти: {exc}"

    invalidate_memory_cache()
    _schedule_git_sync(_MEMORY_FILE, "bot_memory")

    if replaced_text is not None:
        return f"Факт обновлён (заменил: «{replaced_text}»)."
    return "Факт сохранён в память."


async def read_memory_from_obsidian() -> str:
    path = _memory_path()
    try:
        async with aiofiles.open(path, encoding="utf-8") as f:
            content = await f.read()
        return content.strip() or "Файл памяти пуст."
    except FileNotFoundError:
        return f"Файл памяти {_MEMORY_FILE} не найден."
    except OSError as exc:
        logger.error("read_memory_from_obsidian: %s", exc)
        return f"Ошибка чтения файла памяти: {exc}"


async def delete_last_log_entry(filename: str) -> tuple[bool, str]:
    """Delete the most recent entry from _bot/{filename}.

    Entries are prepended right after '## Log', so the first non-empty line
    after that header is always the newest.
    Returns (True, deleted_line) on success, (False, error_message) on failure.
    """
    path = Path(settings.obsidian_vault_path) / "_bot" / filename
    if not path.exists():
        return False, f"Файл '{filename}' не найден в _bot/."

    try:
        async with aiofiles.open(path, encoding="utf-8") as f:
            content = await f.read()

        lines_list = content.splitlines(keepends=True)

        header_idx = next(
            (i for i, ln in enumerate(lines_list) if ln.strip() == _LOG_HEADER),
            None,
        )
        if header_idx is None:
            return False, "Раздел ## Log не найден в файле."

        entry_idx = next(
            (i for i in range(header_idx + 1, len(lines_list)) if lines_list[i].strip()),
            None,
        )
        if entry_idx is None:
            return False, "Нет записей для отмены."

        deleted = lines_list[entry_idx].strip()
        lines_list.pop(entry_idx)

        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write("".join(lines_list))

    except OSError as exc:
        logger.error("delete_last_log_entry: %s", exc)
        return False, f"Ошибка файла: {exc}"

    stem = Path(filename).stem
    _schedule_git_sync(f"_bot/{filename}", stem)
    logger.info("Deleted last log entry from %s", filename)
    return True, deleted


async def read_bot_log(filename: str, lines: int = 15) -> str:
    """Читает последние N строк из _bot/{filename}.

    filename — имя файла с расширением, например 'sleep.md'.
    Используется инструментом read_bot_log для показа пользователю что записалось.
    """
    path = Path(settings.obsidian_vault_path) / "_bot" / filename
    if not path.exists():
        available = sorted(p.name for p in path.parent.glob("*.md")) if path.parent.exists() else []
        hint = f" Доступные файлы: {', '.join(available)}." if available else ""
        return f"Файл '{filename}' не найден в _bot/.{hint}"
    try:
        async with aiofiles.open(path, encoding="utf-8") as f:
            content = await f.read()
        all_lines = [ln for ln in content.splitlines() if ln.strip()]
        last = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return "\n".join(last) if last else "(файл пуст)"
    except OSError as exc:
        logger.error("read_bot_log: %s", exc)
        return f"Ошибка чтения '{filename}': {exc}"


async def append_to_bot_log(filename: str, text: str) -> str:
    """Write a timestamped entry to MyBrain/_bot/{filename}.

    Unlike append_obsidian_log, takes a literal filename (e.g. "health.md")
    without sphere alias resolution — intended for programmatic callers like
    scheduled pings.
    """
    path = Path(settings.obsidian_vault_path) / "_bot" / filename

    if not path.exists():
        # Auto-create with minimal header so new spheres (energy, etc.) work out of the box
        path.parent.mkdir(parents=True, exist_ok=True)
        sphere_name = path.stem.replace("_", " ").capitalize()
        path.write_text(f"# {sphere_name}\n\n## Log\n", encoding="utf-8")
        logger.info("Auto-created log file: %s", filename)

    tz = ZoneInfo(settings.task_timezone)
    timestamp = datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M")
    log_line = f"- [{timestamp}] {text}\n"

    try:
        async with aiofiles.open(path, encoding="utf-8") as f:
            content = await f.read()

        if _LOG_HEADER in content:
            lines = content.splitlines(keepends=True)
            insert_at = next(
                (i + 1 for i, ln in enumerate(lines) if ln.strip() == _LOG_HEADER),
                None,
            )
            if insert_at is not None:
                lines.insert(insert_at, log_line)
                new_content = "".join(lines)
            else:
                new_content = content + log_line
        else:
            sep = "\n" if content and not content.endswith("\n") else ""
            new_content = f"{content}{sep}\n{_LOG_HEADER}\n{log_line}"

        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(new_content)

    except OSError as exc:
        logger.error("append_to_bot_log write: %s", exc)
        return f"Ошибка записи файла: {exc}"

    stem = Path(filename).stem
    entry_id = _write_log_entry(sphere=stem, raw_text=text, logged_at=timestamp)
    if entry_id is not None:
        _schedule_extraction(entry_id, stem, text)
    else:
        logger.warning("log_entry NOT written to SQLite for sphere=%s — check allowed_user_id config", stem)
    _schedule_git_sync(f"_bot/{filename}", stem)
    return "Записано."


def _schedule_git_sync(rel_path: str, sphere: str) -> None:
    """Запускает git-синхронизацию в фоне. Не блокирует ответ пользователю.
    Все ошибки попадают только в системный лог, не в чат."""
    async def _task() -> None:
        async with _GIT_LOCK:          # сериализуем: параллельные записи в очередь
            result = await _git_sync_impl(rel_path, sphere)
            if result == "ok" or result.startswith("ok"):
                logger.debug("Git sync ok: %s", rel_path)
            else:
                logger.warning("Git sync incomplete (%s): %s", rel_path, result)

    asyncio.create_task(_task())


async def _git_sync_impl(rel_path: str, sphere: str) -> str:
    cwd = settings.obsidian_vault_path

    async def _run(cmd: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, (stdout + stderr).decode(errors="replace").strip()

    # Порядок: сначала коммитим наши изменения, потом тянем remote (rebase),
    # потом пушим. Это предотвращает "Your local changes would be overwritten".
    cwd_flag = f"git -C {cwd}"

    # 1. Stage только наш файл
    code, out = await _run(f"{cwd_flag} add {rel_path}")
    if code != 0:
        logger.warning("git add failed: %s", out)
        return f"ошибка git add: {out[:200]}"

    # 2. Commit локальных изменений
    code, out = await _run(f'{cwd_flag} commit -m "bot: log update for {sphere}"')
    if code != 0 and "nothing to commit" not in out.lower():
        logger.warning("git commit failed: %s", out)
        return f"ошибка git commit: {out[:200]}"

    # 3. Pull с rebase — remote-коммиты встают ДО нашего, конфликты редки
    code, out = await _run(f"{cwd_flag} pull --rebase origin main")
    if code != 0:
        logger.warning("git pull --rebase failed: %s", out)
        await _run(f"{cwd_flag} rebase --abort")  # откатываем если застряли
        # Данные уже закоммичены локально — пробуем push нашей версии
        code2, out2 = await _run(f"{cwd_flag} push origin main")
        if code2 != 0:
            return f"сохранено локально, sync failed: {out[:100]}"
        return "ok (pull skipped)"

    # 4. Push
    code, out = await _run(f"{cwd_flag} push origin main")
    if code != 0:
        logger.warning("git push failed: %s", out)
        return f"сохранено локально, push failed: {out[:100]}"

    return "ok"
