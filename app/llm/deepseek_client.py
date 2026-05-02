import json
import logging
from typing import Any

import httpx

from app.config import settings


logger = logging.getLogger(__name__)

_TIMEOUT = 45.0


def call_deepseek(prompt: str) -> dict[str, Any]:
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.deepseek_model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }

    logger.debug("DeepSeek request model=%s", settings.deepseek_model)

    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.post(url, headers=headers, json=payload)

    response.raise_for_status()

    body = response.json()
    content = body["choices"][0]["message"]["content"]

    logger.debug("DeepSeek raw response: %s", content)

    return json.loads(content)
