"""One background-model route: account-login Grok, then the caller's safe default.

Every non-interactive text/JSON call in the core goes through ``text()`` or
``json_object()``. Owner decision 2026-10-31: that call is Grok 4.7 at low
effort through the host CLI. ``KLUKAI_EXTRACTION_DAILY_LIMIT`` still reserves
one UTC-day attempt before anything is sent. If Grok is unavailable, capped,
fails or returns nothing usable, the caller gets its safe default. There is
no Anthropic fallback and no local model.

Never raises into the caller. A call that a higher-priority recall preempts
returns the safe default too, unless the caller passes ``requeue=True`` (the
turn's own extraction, whose queue puts the job back): then it raises
ExtractionCancelled. One INFO line per call names the caller, provider, model
and outcome; never prompt or reply content.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from . import extraction_llm
from .extraction_llm import CallReport
from .llm_json import parse_json

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Adaptive thinking counts against max_tokens. A caller's own budget is for
# the visible reply, so Haiku gets headroom on top. Unused headroom costs
# nothing.
THINKING_HEADROOM_TOKENS = 3072


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
    caller: str,
    prompt: str,
    *,
    max_tokens: int,
    system: str | None,
    role: str = "background",
    timeout: float | None = None,
    requeue: bool = False,
) -> tuple[str | None, str]:
    """One budgeted background request: (visible text or None, outcome)."""
    kwargs: dict[str, Any] = {"max_tokens": max_tokens, "route": caller}
    if role != "background":
        kwargs["role"] = role
    if timeout is not None:
        kwargs["timeout"] = timeout
    if system is not None:
        kwargs["system"] = system
    try:
        return await extraction_llm.complete(prompt, **kwargs), "ok"
    except extraction_llm.ExtractionCancelled:
        if requeue:
            raise
        return None, "cancelled"
    except extraction_llm.ExtractionUnavailable:
        return None, "unavailable"
    except extraction_llm.ExtractionLLMError as error:
        logger.warning("Haiku %s failed: %s", caller, error)
        return None, "failed"
    except Exception as error:  # a background caller must never see a raise
        logger.warning("Haiku %s failed (%s)", caller, type(error).__name__)
        return None, "failed"


async def _route(
    caller: str,
    prompt: str,
    *,
    max_tokens: int,
    system: str | None,
    report: CallReport | None,
    haiku_max_tokens: int | None,
    convert: Callable[[str], _T | None],
    unusable: str,
    role: str = "background",
    timeout: float | None = None,
    requeue: bool = False,
) -> _T | None:
    report = report if report is not None else CallReport()
    report.provider, report.model = extraction_llm.provider(), extraction_llm.model()
    text, reason = await _haiku(
        caller,
        prompt,
        max_tokens=haiku_max_tokens or haiku_budget(max_tokens),
        system=system,
        role=role,
        timeout=timeout,
        requeue=requeue,
    )
    result = convert(text) if text is not None else None
    if text is not None and result is None:
        reason = unusable
    report.outcome = "ok" if result is not None else f"{reason}->defaults"
    logger.info(
        "Background model: caller=%s provider=%s model=%s outcome=%s",
        caller,
        report.provider,
        report.model,
        report.outcome,
    )
    return result


async def text(
    caller: str,
    prompt: str,
    *,
    max_tokens: int,
    system: str | None = None,
    report: CallReport | None = None,
    haiku_max_tokens: int | None = None,
    role: str = "background",
    requeue: bool = False,
) -> str:
    """Plain text for ``caller``; ``""`` when nothing usable came back.

    ``prompt``/``system`` go to Haiku (the default background instructions
    when ``system`` is None). ``max_tokens`` is the caller's visible-reply
    budget.
    """
    result = await _route(
        caller,
        prompt,
        max_tokens=max_tokens,
        system=system,
        report=report,
        haiku_max_tokens=haiku_max_tokens,
        convert=_usable_text,
        unusable="empty",
        role=role,
        requeue=requeue,
    )
    return result or ""


async def json_object(
    caller: str,
    prompt: str,
    *,
    max_tokens: int,
    system: str | None = None,
    report: CallReport | None = None,
    haiku_max_tokens: int | None = None,
    role: str = "background",
    timeout: float | None = None,
    requeue: bool = False,
) -> dict[str, Any] | None:
    """A non-empty JSON object for ``caller``; None means use your defaults.

    Haiku's reply is parsed tolerantly (code fences, prose around the object,
    trailing commas).
    """
    return await _route(
        caller,
        prompt,
        max_tokens=max_tokens,
        system=system,
        report=report,
        haiku_max_tokens=haiku_max_tokens,
        convert=_parsed_object,
        unusable="unparsable",
        role=role,
        timeout=timeout,
        requeue=requeue,
    )
