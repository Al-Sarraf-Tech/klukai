"""One background-model route: Claude Haiku first, then Dominus, then defaults.

Every non-interactive text/JSON call in the core goes through ``text()`` or
``json_object()``: the per-turn extraction and compaction, promise/event
detection, episode journal entries, proactive lines (dreams, memory recall,
missions, the evening romance line and recap), the weekly reflection, return
greetings, Her POV and memory-archive captions when creative text is local,
and the legacy affection classifier. Paths on the capped creative-text route
(app/creative_text.py) keep it; only their local branch comes here.

``KLUKAI_EXTRACTION_PROVIDER`` is the one switch for all of it. With
``anthropic`` a call goes to Claude Haiku at high effort through
app/extraction_llm.py, which reserves an attempt in the shared UTC-day ledger
(``KLUKAI_EXTRACTION_DAILY_LIMIT``) before anything is sent. If Haiku is
unavailable, capped, fails or returns nothing usable, the caller's original
local request runs, but only while the Dominus circuit is closed; otherwise
the caller gets its safe default. With ``local`` (the default) the caller's
local request runs exactly as before.

Never raises into the caller. One INFO line per call names the caller,
provider, model and outcome; never prompt or reply content.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from . import extraction_llm
from .extraction_llm import CallReport
from .llm_json import parse_json

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# The local model every migrated caller used; only for the log line.
LOCAL_MODEL = "cognitivecomputations_dolphin-mistral-24b-venice-edition"
# Adaptive thinking counts against max_tokens. A caller's own budget is for
# the visible reply (what the local model gets), so Haiku gets headroom on
# top. Unused headroom costs nothing.
THINKING_HEADROOM_TOKENS = 3072
# Wall-clock bounds on a local request, including the wait for the shared LM
# gate (an unbounded asyncio.Lock). As Haiku's fallback it has to be quick to
# be worth anything; as the only provider it keeps its request timeouts, with
# this as a backstop so a stuck gate can never park a background step forever.
LOCAL_FALLBACK_DEADLINE_SECONDS = 45.0
LOCAL_PRIMARY_DEADLINE_SECONDS = 300.0


def local_circuit_closed() -> bool:
    """True when the Dominus breaker would admit a request right now."""
    from .circuit_breakers import State, get_breaker

    return get_breaker("lm_studio").state is State.CLOSED


def haiku_budget(max_tokens: int) -> int:
    """The Haiku max_tokens for a caller's visible-reply budget."""
    return max(
        1,
        min(
            extraction_llm.MAX_REQUEST_TOKENS,
            max_tokens + THINKING_HEADROOM_TOKENS,
        ),
    )


def _usable_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _usable_object(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) and value else None


def _parsed_object(text: str) -> dict[str, Any] | None:
    return _usable_object(parse_json(text))


async def _haiku(
    caller: str, prompt: str, *, max_tokens: int, system: str | None
) -> tuple[str | None, str]:
    """One budgeted Haiku request: (visible text or None, outcome)."""
    kwargs: dict[str, Any] = {"max_tokens": max_tokens, "route": caller}
    if system is not None:
        kwargs["system"] = system
    try:
        return await extraction_llm.complete(prompt, **kwargs), "ok"
    except extraction_llm.ExtractionUnavailable:
        return None, "unavailable"
    except extraction_llm.ExtractionLLMError as error:
        logger.warning("Haiku %s failed: %s", caller, error)
        return None, "failed"
    except Exception as error:  # a background caller must never see a raise
        logger.warning("Haiku %s failed (%s)", caller, type(error).__name__)
        return None, "failed"


async def _local(
    caller: str, local: Callable[[], Awaitable[object]], deadline: float
) -> object:
    try:
        async with asyncio.timeout(deadline):
            return await local()
    except Exception as error:  # TimeoutError included
        logger.warning("Local %s failed (%s)", caller, type(error).__name__)
        return None


async def _route(
    caller: str,
    prompt: str,
    *,
    max_tokens: int,
    local: Callable[[], Awaitable[object]],
    local_model: str,
    system: str | None,
    report: CallReport | None,
    haiku_max_tokens: int | None,
    from_haiku: Callable[[str], _T | None],
    from_local: Callable[[object], _T | None],
    unusable: str,
) -> _T | None:
    report = report if report is not None else CallReport()
    result: _T | None = None
    if extraction_llm.uses_anthropic():
        report.provider, report.model = "anthropic", extraction_llm.model()
        text, reason = await _haiku(
            caller,
            prompt,
            max_tokens=haiku_max_tokens or haiku_budget(max_tokens),
            system=system,
        )
        if text is not None:
            result = from_haiku(text)
            if result is None:
                reason = unusable
        if result is not None:
            report.outcome = "ok"
        else:
            if local_circuit_closed():
                report.provider, report.model = "local", local_model
                result = from_local(
                    await _local(caller, local, LOCAL_FALLBACK_DEADLINE_SECONDS)
                )
            report.outcome = (
                f"{reason}->{'local' if result is not None else 'defaults'}"
            )
    else:
        report.provider, report.model = "local", local_model
        result = from_local(await _local(caller, local, LOCAL_PRIMARY_DEADLINE_SECONDS))
        report.outcome = "ok" if result is not None else "defaults"
    logger.info(
        "Background model: caller=%s provider=%s model=%s outcome=%s",
        caller,
        report.provider,
        report.model or "-",
        report.outcome,
    )
    return result


async def text(
    caller: str,
    prompt: str,
    *,
    max_tokens: int,
    local: Callable[[], Awaitable[str]],
    local_model: str = LOCAL_MODEL,
    system: str | None = None,
    report: CallReport | None = None,
    haiku_max_tokens: int | None = None,
) -> str:
    """Plain text for ``caller``; ``""`` when nothing usable came back.

    ``prompt``/``system`` go to Haiku (the default background instructions
    when ``system`` is None); ``local`` is the caller's original local request,
    run as the fallback, or as the whole call with the local provider.
    ``max_tokens`` is the caller's visible-reply budget.
    """
    result = await _route(
        caller,
        prompt,
        max_tokens=max_tokens,
        local=local,
        local_model=local_model,
        system=system,
        report=report,
        haiku_max_tokens=haiku_max_tokens,
        from_haiku=_usable_text,
        from_local=_usable_text,
        unusable="empty",
    )
    return result or ""


async def json_object(
    caller: str,
    prompt: str,
    *,
    max_tokens: int,
    local: Callable[[], Awaitable[object]],
    local_model: str = LOCAL_MODEL,
    system: str | None = None,
    report: CallReport | None = None,
    haiku_max_tokens: int | None = None,
) -> dict[str, Any] | None:
    """A non-empty JSON object for ``caller``; None means use your defaults.

    Haiku's reply is parsed with the same tolerant parser as the local path
    (code fences, prose around the object, trailing commas).
    """
    return await _route(
        caller,
        prompt,
        max_tokens=max_tokens,
        local=local,
        local_model=local_model,
        system=system,
        report=report,
        haiku_max_tokens=haiku_max_tokens,
        from_haiku=_parsed_object,
        from_local=_usable_object,
        unusable="unparsable",
    )
