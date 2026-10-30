"""Separately capped creative writing on the shared Grok account route.

The legacy ``anthropic`` setting retains the dream/Her POV attempt cap. Every
background model call uses Grok 4.7 at medium effort and the shared daily cap.
There is no Anthropic call. Failed attempts remain consumed.
"""

from __future__ import annotations

import os

from . import background_llm, extraction_llm
from .creative_budget import reserve_attempt


class CreativeTextError(RuntimeError):
    """A credential-free, terminal creative-writing failure."""


def uses_cloud_text() -> bool:
    """True for separately capped writing through Haiku.

    ``anthropic`` keeps the legacy creative cap; ``haiku`` and its old name
    ``local`` use the ordinary background route. No local model exists.
    """
    provider = os.environ.get("KLUKAI_CREATIVE_TEXT_PROVIDER", "haiku").strip().lower()
    if provider not in {"haiku", "local", "anthropic"}:
        raise CreativeTextError("Unknown creative text provider")
    return provider == "anthropic"


async def complete_creative_text(system: str, user: str, max_tokens: int = 1024) -> str:
    """One creative attempt, subject to both creative and shared Haiku caps."""
    if not uses_cloud_text():
        raise CreativeTextError("Cloud creative text is disabled")
    from .grok_chat import configured

    if not configured():
        raise CreativeTextError("Grok account chat is not configured")
    if (
        not isinstance(system, str)
        or not system.strip()
        or len(system) > extraction_llm.MAX_SYSTEM_CHARS
        or not isinstance(user, str)
        or not user.strip()
        or len(user) > extraction_llm.MAX_PROMPT_CHARS
        or type(max_tokens) is not int
        or not 1 <= max_tokens <= extraction_llm.MAX_REQUEST_TOKENS
    ):
        raise CreativeTextError("Invalid creative text request")
    # Preserve the original creative allowance before the shared route reserves
    # its own attempt. Either refusal prevents HTTP; failures never refund caps.
    await reserve_attempt("text")
    result = await background_llm.text(
        "creative_text", user, system=system, max_tokens=max_tokens
    )
    if not result:
        raise CreativeTextError("Creative text request failed")
    return result.strip()
