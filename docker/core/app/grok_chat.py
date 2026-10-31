"""Account-login Grok chat through the host worker.

The worker runs the official CLI with GROK_AUTH_PATH. This module sends the
prompt over the existing private socket and never reads an API key or calls
api.x.ai.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from .dependencies import http_stream


class GrokChatError(RuntimeError):
    """A credential-free chat failure."""


class Turn:
    def __init__(self, text: str, model: str, effort: str, effort_source: str):
        self.text = text
        self.model = model
        self.effort = effort
        self.effort_source = effort_source


MODEL = "grok-4.7"
EFFORT = "medium"


def configured() -> bool:
    """True when the host worker socket and its token are present."""
    socket = Path(os.environ.get("KLUKAI_AGY_SOCKET", "/run/klukai-agy/image.sock"))
    token = Path(os.environ.get("KLUKAI_AGY_TOKEN_FILE", "/run/klukai-agy/token"))
    try:
        return socket.is_socket() and token.is_file()
    except OSError:
        return False


def _settings() -> tuple[str, str]:
    from .agy_image_provider import _settings as image_settings

    return image_settings()


async def complete_turn(
    system: str, prompt: str, *, timeout: float = 120.0, role: str = "chat"
) -> Turn:
    if not configured():
        raise GrokChatError("Grok account chat is not configured")
    socket, token = _settings()
    payload = {"system": system, "prompt": prompt, "role": role}
    try:
        transport = httpx.AsyncHTTPTransport(uds=socket, retries=0, trust_env=False)
        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout, connect=5.0),
        ) as client:
            async with http_stream(
                "grok_chat",
                client,
                "POST",
                "http://klukai-agy/complete",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            ) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1_000_000:
                        raise GrokChatError("Grok chat response is too large")
                status = response.status_code
    except (OSError, httpx.HTTPError, TimeoutError) as exc:
        raise GrokChatError("Grok chat is unavailable") from exc
    if status != 200:
        raise GrokChatError("Grok chat failed")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise GrokChatError("Grok chat returned malformed JSON") from exc
    text, model, effort = data.get("text"), data.get("model"), data.get("effort")
    source = data.get("effort_source")
    if (
        not isinstance(text, str)
        or not text.strip()
        or model != MODEL
        or effort != EFFORT
        or source not in {"stream", "summary", "flag"}
    ):
        raise GrokChatError("Grok chat model or effort was unexpected")
    return Turn(text.strip(), model, effort, source)
