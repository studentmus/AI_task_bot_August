import asyncio
import logging
from datetime import datetime
from pathlib import Path

import aiofiles

from app.config import settings

logger = logging.getLogger(__name__)

# Используем английский заголовок, как в твоем файле
_LOG_HEADER = "## Log"

def _sphere_path(sphere: str) -> Path:
    # Динамически берем путь: локально будет /home/..., на сервере /opt/...
    return Path(settings.obsidian_vault_path) / "_bot" / f"{sphere}.md"

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
    path = _sphere_path(sphere)
    path.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    log_line = f"- [{timestamp}] {entry}\n"

    try:
        content = ""
        try:
            async with aiofiles.open(path, encoding="utf-8") as f:
                content = await f.read()
        except FileNotFoundError:
            pass

        if _LOG_HEADER in content:
            lines = content.splitlines(keepends=True)
            insert_at = next(
                (i + 1 for i, l in enumerate(lines) if l.strip() == _LOG_HEADER),
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

    git_result = await _git_sync(f"_bot/{sphere}.md", sphere)
    return f"Запись добавлена. Git: {git_result}"

async def _git_sync(rel_path: str, sphere: str) -> str:
    cwd = settings.obsidian_vault_path

    async def _run(cmd: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, (stdout + stderr).decode(errors="replace").strip()

    # Жесткий переход в нужную папку перед каждой командой
    steps = [
        ("pull",   f"cd {cwd} && git pull origin main"),
        ("add",    f"cd {cwd} && git add {rel_path}"),
        ("commit", f'cd {cwd} && git commit -m "bot: log update for {sphere}"'),
        ("push",   f"cd {cwd} && git push origin main"),
    ]

    for step_name, cmd in steps:
        code, out = await _run(cmd)
        if code != 0:
            if step_name == "commit" and "nothing to commit" in out.lower():
                continue
            logger.warning("git %s failed (code=%d): %s", step_name, code, out)
            return f"ошибка на шаге '{step_name}': {out[:200]}"

    return "ok"
