import asyncio
import logging
from datetime import datetime
from pathlib import Path

import aiofiles

from app.config import settings

logger = logging.getLogger(__name__)

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

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
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

    git_result = await _git_sync(f"_bot/{resolved}.md", resolved)
    return f"Запись добавлена. Git: {git_result}"


async def _git_sync(rel_path: str, sphere: str) -> str:
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

    # Двойная защита: cwd= в Python + флаг -C в самом git
    steps = [
        ("pull",   f"git -C {cwd} pull origin main"),
        ("add",    f"git -C {cwd} add {rel_path}"),
        ("commit", f'git -C {cwd} commit -m "bot: log update for {sphere}"'),
        ("push",   f"git -C {cwd} push origin main"),
    ]

    for step_name, cmd in steps:
        code, out = await _run(cmd)
        if code != 0:
            if step_name == "commit" and "nothing to commit" in out.lower():
                continue
            logger.warning("git %s failed (code=%d): %s", step_name, code, out)
            return f"ошибка на шаге '{step_name}': {out[:200]}"

    return "ok"
