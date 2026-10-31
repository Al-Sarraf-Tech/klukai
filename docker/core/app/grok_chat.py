"""Account-login Grok chat through the host worker.

The worker runs the official CLI with GROK_AUTH_PATH. This module sends the
prompt over the existing private socket and never reads an API key or calls
api.x.ai.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from .dependencies import http_stream


class GrokChatError(RuntimeError):
    """A credential-free chat failure."""


class GrokChatCancelled(GrokChatError):
    """The worker dropped this call so a higher-priority one could run."""


class Turn:
    def __init__(self, text: str, model: str, effort: str, effort_source: str):
        self.text = text
        self.model = model
        self.effort = effort
        self.effort_source = effort_source


MODEL = "grok-4.7"
# Owner decision 2026-10-31: the chat reply is low effort (fastest).
# Override with KLUKAI_GROK_CHAT_EFFORT. Background text has its own default.
EFFORT = "low"
_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}


def chat_effort() -> str:
    value = os.environ.get("KLUKAI_GROK_CHAT_EFFORT", EFFORT).strip().lower()
    return value if value in _EFFORTS else EFFORT


def background_effort() -> str:
    value = os.environ.get("KLUKAI_GROK_BACKGROUND_EFFORT", "low").strip().lower()
    return value if value in _EFFORTS else "low"


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


def _expected(role: str) -> str:
    return chat_effort() if role == "chat" else background_effort()


def _event(obj: dict[str, Any]) -> dict[str, Any]:
    if obj.get("type") in {"delta", "done"}:
        return obj
    if obj.get("cancelled") is True:
        return {"type": "done", "cancelled": True, "text": ""}
    if "text" in obj or "model" in obj:
        return {"type": "done", **obj}
    raise GrokChatError("Grok chat returned malformed JSON")


async def iter_events(
    system: str, prompt: str, *, timeout: float = 120.0, role: str = "chat"
) -> AsyncIterator[dict[str, Any]]:
    """Yield delta text, then a done event, as the worker streams them."""
    if not configured():
        raise GrokChatError("Grok account chat is not configured")
    socket, token = _settings()
    effort = _expected(role)
    payload = {"system": system, "prompt": prompt, "role": role, "effort": effort}
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
                status = response.status_code
                if status == 409:
                    raise GrokChatCancelled("cancelled")
                if status != 200:
                    raise GrokChatError("Grok chat failed")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1_000_000:
                        raise GrokChatError("Grok chat response is too large")
                    while b"\n" in raw:
                        line, raw = raw.split(b"\n", 1)
                        if line.strip():
                            yield _event(json.loads(line))
                if raw.strip():
                    yield _event(json.loads(raw))
    except GrokChatError:
        raise
    except ValueError as exc:
        raise GrokChatError("Grok chat returned malformed JSON") from exc
    except (OSError, httpx.HTTPError, TimeoutError) as exc:
        raise GrokChatError("Grok chat is unavailable") from exc


async def iter_text(
    system: str, prompt: str, *, timeout: float = 120.0, role: str = "chat"
) -> AsyncIterator[str]:
    """Yield reply text as it arrives. The last event is not yielded as text."""
    async for event in iter_events(system, prompt, timeout=timeout, role=role):
        if event.get("cancelled") is True:
            raise GrokChatCancelled("cancelled")
        if event.get("type") == "delta" and isinstance(event.get("text"), str):
            yield event["text"]
        elif event.get("type") == "done":
            _turn_from(event, role)
            return


def _turn_from(data: dict[str, Any], role: str) -> Turn:
    if data.get("cancelled") is True:
        raise GrokChatCancelled("cancelled")
    text, model, effort = data.get("text"), data.get("model"), data.get("effort")
    source = data.get("effort_source")
    if (
        not isinstance(text, str)
        or not text.strip()
        or model != MODEL
        or effort != _expected(role)
        or source not in {"stream", "summary", "flag"}
    ):
        raise GrokChatError("Grok chat model or effort was unexpected")
    return Turn(text.strip(), model, effort, source)


async def complete_turn(
    system: str, prompt: str, *, timeout: float = 120.0, role: str = "chat"
) -> Turn:
    if not configured():
        raise GrokChatError("Grok account chat is not configured")
    turn: Turn | None = None
    async for event in iter_events(system, prompt, timeout=timeout, role=role):
        if event.get("type") == "done" or (
            event.get("type") not in {"delta", "done"} and "model" in event
        ):
            turn = _turn_from(event, role)
    if turn is None:
        raise GrokChatError("Grok chat returned no text")
    return turn
