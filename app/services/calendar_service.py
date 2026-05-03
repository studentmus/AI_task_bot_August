import requests
from datetime import datetime
from icalendar import Calendar, Event
from app.config import settings
import logging

logger = logging.getLogger(__name__)

class CalendarService:
    def __init__(self):
        self.url = settings.RADICALE_URL
        self.auth = (settings.RADICALE_USER, settings.RADICALE_PASSWORD)
    
    def sync_task(self, task_id: int, title: str, dt: datetime, category: str = None):
        """One-way sync: push task to Radicale calendar"""
        try:
            cal = Calendar()
            event = Event()
            event.add('summary', title)
            event.add('dtstart', dt)
            event.add('dtend', dt)
            event.add('uid', f'task-{task_id}@taskbot')
            if category:
                event.add('categories', category)
            
            cal.add_component(event)
            
            response = requests.put(
                f"{self.url}{task_id}.ics",
                data=cal.to_ical(),
                auth=self.auth,
                headers={'Content-Type': 'text/calendar'}
            )
            response.raise_for_status()
            logger.info(f"Task {task_id} synced to calendar")
        except Exception as e:
            logger.error(f"Calendar sync failed: {e}")
