import json
import logging
import time
from typing import Any

import httpx

from app.config import settings


logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_MAX_ATTEMPTS = 3
_RETRY_STATUSES = {429, 503}
_RETRY_DELAYS = (2.0, 5.0)  # секунды между попытками 1→2, 2→3


def _post_json(payload: dict[str, Any]) -> dict[str, Any]:
    """POST к /chat/completions с retry на 429/503 и сетевые ошибки."""
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    last_exc: Exception | None = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            logger.debug("DeepSeek attempt %d/%d", attempt, _MAX_ATTEMPTS)
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.post(url, headers=headers, json=payload)

            if response.status_code in _RETRY_STATUSES:
                logger.warning("DeepSeek HTTP %s attempt %d/%d", response.status_code, attempt, _MAX_ATTEMPTS)
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(_RETRY_DELAYS[attempt - 1])
                    continue
                response.raise_for_status()

            response.raise_for_status()
            return response.json()

        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_exc = exc
            logger.warning("DeepSeek %s attempt %d/%d", type(exc).__name__, attempt, _MAX_ATTEMPTS)
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_DELAYS[attempt - 1])

        except (httpx.HTTPStatusError, json.JSONDecodeError, KeyError):
            raise

    raise RuntimeError(f"DeepSeek недоступен после {_MAX_ATTEMPTS} попыток") from last_exc


def call_deepseek_parse(prompt: str) -> dict[str, Any]:
    """Structured JSON parsing: temperature=0, response_format=json_object."""
    payload = {
        "model": settings.deepseek_model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    body = _post_json(payload)
    content = body["choices"][0]["message"]["content"]
    logger.debug("DeepSeek parse response: %s", content)
    return json.loads(content)


def call_deepseek_chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Chat completion: возвращает полный message-объект (role, content, tool_calls?)."""
    payload: dict[str, Any] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": 0.2,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    body = _post_json(payload)
    msg = body["choices"][0]["message"]
    logger.debug(
        "DeepSeek chat response: role=%s tool_calls=%d",
        msg.get("role"),
        len(msg.get("tool_calls") or []),
    )
    return msg
