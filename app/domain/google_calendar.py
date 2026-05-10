import logging
from datetime import date, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import settings


logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_DATA_DIR = Path("data")
_CREDENTIALS_FILE = _DATA_DIR / "credentials.json"
_TOKEN_FILE = _DATA_DIR / "token.json"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_credentials() -> Credentials:
    creds: Credentials | None = None

    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(_CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)
        _TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return creds


def _get_service():
    return build("calendar", "v3", credentials=_get_credentials())


# ---------------------------------------------------------------------------
# Вспомогательное: формирование тела события
# ---------------------------------------------------------------------------

def _build_event_body(task) -> dict:
    """Строит тело события Google Calendar из объекта Task."""
    body: dict = {
        "summary": task.text,
        "description": f"Создано через AI Task Bot. Категория: {task.category}",
        # Храним local_task_id чтобы находить событие без обращения к SQLite
        "extendedProperties": {
            "private": {"local_task_id": str(task.id)}
        },
    }

    try:
        t_date = date.fromisoformat(task.suggested_date)
    except (ValueError, TypeError):
        t_date = date.today()

    if task.all_day or not task.event_time:
        body["start"] = {"date": t_date.isoformat()}
        body["end"] = {"date": (t_date + timedelta(days=1)).isoformat()}
    else:
        tz = settings.task_timezone
        try:
            if "-" in task.event_time:
                start_str, end_str = task.event_time.split("-", 1)
                start_dt = f"{t_date.isoformat()}T{start_str.strip()}:00"
                end_dt = f"{t_date.isoformat()}T{end_str.strip()}:00"
            else:
                h, m = map(int, task.event_time.split(":"))
                end_h, end_m = divmod(h * 60 + m + 60, 60 * 24)  # +1 час, не выходя за сутки
                end_h = (h + 1) % 24
                start_dt = f"{t_date.isoformat()}T{h:02d}:{m:02d}:00"
                end_dt = f"{t_date.isoformat()}T{end_h:02d}:{m:02d}:00"
        except (ValueError, AttributeError):
            # Fallback: весь день
            body["start"] = {"date": t_date.isoformat()}
            body["end"] = {"date": (t_date + timedelta(days=1)).isoformat()}
            return body

        body["start"] = {"dateTime": start_dt, "timeZone": tz}
        body["end"] = {"dateTime": end_dt, "timeZone": tz}

    return body


def _find_event_id(service, local_task_id: int) -> str | None:
    """Ищет событие в Google Calendar по local_task_id в extendedProperties."""
    try:
        result = service.events().list(
            calendarId="primary",
            privateExtendedProperty=f"local_task_id={local_task_id}",
            maxResults=1,
            singleEvents=True,
        ).execute()
        items = result.get("items", [])
        return items[0]["id"] if items else None
    except Exception as exc:
        logger.error("_find_event_id failed for local_task_id=%s: %s", local_task_id, exc)
        return None


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def create_event(task) -> str | None:
    """Создаёт событие в Google Calendar. Возвращает event_id или None при ошибке."""
    try:
        service = _get_service()
        body = _build_event_body(task)
        event = service.events().insert(calendarId="primary", body=body).execute()
        event_id = event.get("id")
        logger.info("Google Calendar event created: event_id=%s task_id=%s", event_id, task.id)
        return event_id
    except Exception as exc:
        logger.error("create_event failed for task_id=%s: %s", task.id, exc)
        return None


def delete_event(local_task_id: int) -> bool:
    """Удаляет событие из Google Calendar по local_task_id."""
    try:
        service = _get_service()
        event_id = _find_event_id(service, local_task_id)
        if not event_id:
            logger.warning("delete_event: no event found for local_task_id=%s", local_task_id)
            return False
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        logger.info("Google Calendar event deleted: event_id=%s local_task_id=%s", event_id, local_task_id)
        return True
    except Exception as exc:
        logger.error("delete_event failed for local_task_id=%s: %s", local_task_id, exc)
        return False


def mark_event_done(local_task_id: int) -> bool:
    """Добавляет '✅ ' к названию события и красит его в серый (colorId='8')."""
    try:
        service = _get_service()
        event_id = _find_event_id(service, local_task_id)
        if not event_id:
            logger.warning("mark_event_done: no event found for local_task_id=%s", local_task_id)
            return False

        event = service.events().get(calendarId="primary", eventId=event_id).execute()
        summary = event.get("summary", "")
        if not summary.startswith("✅ "):
            event["summary"] = f"✅ {summary}"
        event["colorId"] = "8"  # Graphite / серый

        service.events().update(
            calendarId="primary", eventId=event_id, body=event
        ).execute()
        logger.info("Google Calendar event marked done: event_id=%s local_task_id=%s", event_id, local_task_id)
        return True
    except Exception as exc:
        logger.error("mark_event_done failed for local_task_id=%s: %s", local_task_id, exc)
        return False
