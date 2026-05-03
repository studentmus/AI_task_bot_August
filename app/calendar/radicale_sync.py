import logging
import uuid
import httpx
from datetime import datetime, date, timedelta
from app.config import settings

logger = logging.getLogger(__name__)

def _format_ical(task) -> str:
    """Формирует тело .ics файла для VEVENT."""
    uid = task.radicale_uid or str(uuid.uuid4())
    now_ts = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    
    # Парсим дату задачи
    try:
        t_date = date.fromisoformat(task.suggested_date)
    except (ValueError, TypeError):
        t_date = date.today()

    if task.all_day or not task.event_time:
        # All-day event
        dt_start = t_date.strftime("%Y%m%d")
        dt_end = (t_date + timedelta(days=1)).strftime("%Y%m%d")
        dt_params = "VALUE=DATE"
    else:
        # Time-specific event
        try:
            h, m = map(int, task.event_time.split(":"))
            dt_start = t_date.strftime("%Y%m%d") + f"T{h:02d}{m:02d}00"
            dt_end = t_date.strftime("%Y%m%d") + f"T{(h+1):02d}{m:02d}00"
            dt_params = "VALUE=DATE-TIME"
        except (ValueError, AttributeError):
            dt_start = t_date.strftime("%Y%m%d")
            dt_end = (t_date + timedelta(days=1)).strftime("%Y%m%d")
            dt_params = "VALUE=DATE"

    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//AI Task Bot//RU
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{now_ts}
DTSTART;{dt_params}:{dt_start}
DTEND;{dt_params}:{dt_end}
SUMMARY:{task.text}
DESCRIPTION:Создано через AI Task Bot. Категория: {task.category}
END:VEVENT
END:VCALENDAR""".replace('\n', '\r\n')

def sync_task(task) -> str | None:
    """Отправляет задачу на Radicale через PUT. Возвращает UID."""
    uid = task.radicale_uid or str(uuid.uuid4())
    url = f"{settings.radicale_url.rstrip('/')}/{uid}.ics"
    content = _format_ical(task)
    
    auth = (settings.radicale_user, settings.radicale_pass)
    
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.put(url, content=content, auth=auth)
            response.raise_for_status()
            logger.info("Successfully synced task %s to Radicale", task.id)
            return uid
    except Exception as e:
        logger.error("Failed to sync task %s to Radicale: %s", task.id, e)
        return None

def delete_task_calendar(uid: str) -> bool:
    """Удаляет задачу из календаря через DELETE."""
    if not uid:
        return False
    url = f"{settings.radicale_url.rstrip('/')}/{uid}.ics"
    auth = (settings.radicale_user, settings.radicale_pass)
    
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.delete(url, auth=auth)
            return response.status_code in (200, 204, 404)
    except Exception:
        return False