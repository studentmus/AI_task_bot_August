#!/usr/bin/env python3
"""One-time Google Calendar OAuth setup for headless servers.

Run from /opt/AI_task_bot_August:
    python scripts/auth_gcal.py

Flow:
  1. Prints an authorization URL
  2. You open it in any browser on any device
  3. After authorizing, browser tries to redirect to http://localhost:1/?code=...
     and shows "Connection refused" — that is expected
  4. Copy the FULL URL from the browser address bar and paste it here
  5. token.json is saved; restart the bot
"""
import sys
from pathlib import Path

# Run from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from google_auth_oauthlib.flow import Flow

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS = Path("data/credentials.json")
TOKEN = Path("data/token.json")

if not CREDENTIALS.exists():
    print(f"ERROR: {CREDENTIALS} not found. Copy credentials.json to data/ first.")
    sys.exit(1)

# Port 1 is guaranteed to be refused — browser shows the full redirect URL with code
REDIRECT_URI = "http://localhost:1"

flow = Flow.from_client_secrets_file(
    str(CREDENTIALS),
    scopes=SCOPES,
    redirect_uri=REDIRECT_URI,
)
auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

print("\n" + "=" * 60)
print("Открой этот URL в браузере (на любом устройстве):")
print("=" * 60)
print(auth_url)
print("=" * 60)
print("\nПосле авторизации браузер покажет ошибку 'Connection refused'.")
print("Скопируй ВЕСЬ URL из адресной строки браузера")
print("(начинается с http://localhost:1/?state=...&code=...)")
print()

redirect_response = input("Вставь URL сюда: ").strip()

try:
    flow.fetch_token(authorization_response=redirect_response)
except Exception as exc:
    print(f"\nERROR: {exc}")
    print("Убедись что скопировал ВЕСЬ URL из адресной строки, включая http://localhost:1/...")
    sys.exit(1)

TOKEN.write_text(flow.credentials.to_json(), encoding="utf-8")
print(f"\nГотово! token.json сохранён в {TOKEN}")
print("Перезапусти бота: systemctl restart taskbot")
