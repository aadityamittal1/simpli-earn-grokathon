"""Shared Grok client (xAI, OpenAI-compatible).

The key is read only from ``XAI_API_KEY``. The model is ``GROK_MODEL`` when set,
otherwise ``grok-4.5``. Both live in ``RAG/.env``, so a teammate can use their own
key by editing that file. This client does not call any other provider.
"""

from __future__ import annotations

import json
import os
import re

API_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = "grok-4.5"


class GrokError(RuntimeError):
    """The Grok request failed or the response was not usable JSON."""


class GrokNotConfigured(GrokError):
    """XAI_API_KEY is unset. Callers should serve cached or saved coverage instead of crashing."""


def api_key() -> str:
    return (os.getenv("XAI_API_KEY") or "").strip()


def model_name() -> str:
    return (os.getenv("GROK_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def is_configured() -> bool:
    return bool(api_key())


def post_chat(payload: dict, api_key_value: str, timeout: float = 90) -> tuple[int, str]:
    """POST one chat completion. Tests replace this; nothing else in the repo should."""
    import requests

    try:
        response = requests.post(
            API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key_value}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise GrokError(f"Grok request failed: {exc}") from exc
    return response.status_code, response.text


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for part in content:
            if isinstance(part, dict):
                pieces.append(str(part.get("text") or ""))
            else:
                pieces.append(str(part))
        return "".join(pieces)
    return str(content or "")


def parse_json_content(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def chat_json(system: str, user: str, timeout: float = 90) -> dict:
    """Ask Grok for one JSON object. Raises GrokNotConfigured when XAI_API_KEY is missing."""
    key = api_key()
    if not key:
        raise GrokNotConfigured("XAI_API_KEY is not set")
    status, text = post_chat(
        {
            "model": model_name(),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        },
        key,
        timeout,
    )
    if status >= 400:
        raise GrokError(f"Grok HTTP {status}: {text[:240]}")
    try:
        body = json.loads(text)
        content = _message_text(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise GrokError("Grok returned an unreadable response") from exc
    if not content.strip():
        raise GrokError("Grok returned an empty response")
    try:
        return parse_json_content(content)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GrokError("Grok did not return JSON") from exc
