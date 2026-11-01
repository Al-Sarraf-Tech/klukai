"""One background-model route: Claude Haiku, the Grok CLI, then the safe default.

Every non-interactive text/JSON call in the core goes through ``text()`` or
``json_object()``. Owner decision 2026-11-01: with Claude switched on, that call
is Claude Haiku 5.5 at low effort. If Haiku fails or returns nothing usable, it
goes to Grok 4.7 at low effort through the host CLI. Her POV stories try the xAI
API before either. ``KLUKAI_EXTRACTION_DAILY_LIMIT`` reserves one UTC-day attempt
before each request. If everything is unavailable, capped, fails or returns
nothing usable, the caller gets its safe default. There is no local model.

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


# Her POV story and episode summaries try the xAI API first. Extraction,
# compaction, promises and the recall pick do not use that key.
_API_CALLERS = frozenset({"her_pov", "episode_summary"})


async def _from_api(
    caller: str,
    prompt: str,
    *,
    system: str | None,
    convert: Callable[[str], _T | None],
) -> tuple[_T | None, str | None]:
    """(result, fallback reason). No key means the API was not attempted."""
    from .xai_chat import XaiCapError, XaiChatError, complete_text
    from .xai_spend import CHAT_MODEL, api_key

    if not api_key():
        return None, None
    system_text = extraction_llm.SYSTEM_PROMPT if system is None else system
    try:
        text = await complete_text(system_text, prompt, max_tokens=8192, timeout=120.0)
    except XaiCapError:
        reason = "capped->cli"
    except XaiChatError as error:
        # Its type and HTTP status only: never its text.
        logger.warning(
            "xAI %s failed (%s, HTTP %s)", caller, type(error).__name__, error.status
        )
        reason = "failed->cli"
    except Exception as error:
        logger.warning("xAI %s failed (%s)", caller, type(error).__name__)
        reason = "failed->cli"
    else:
        result = convert(text) if text else None
        if result is not None:
            return result, None
        reason = "unusable->cli"
    logger.info(
        "Background model: caller=%s provider=xai model=%s outcome=%s",
        caller,
        CHAT_MODEL,
        reason,
    )
    return None, reason


async def _from_claude(
    caller: str,
    prompt: str,
    *,
    max_tokens: int,
    system: str | None,
    convert: Callable[[str], _T | None],
    unusable: str,
    report: CallReport,
    role: str,
    timeout: float | None,
) -> _T | None:
    """One Haiku request. None means the CLI takes the call."""
    kwargs: dict[str, Any] = {"max_tokens": max_tokens, "route": caller}
    if role != "background":
        kwargs["role"] = role
    if timeout is not None:
        kwargs["timeout"] = timeout
    if system is not None:
        kwargs["system"] = system
    result: _T | None = None
    try:
        text = await extraction_llm.complete_claude(prompt, **kwargs)
    except extraction_llm.ExtractionUnavailable:
        outcome = "unavailable->cli"
    except Exception as error:  # a background caller must never see a raise
        logger.warning("Haiku %s failed (%s)", caller, type(error).__name__)
        outcome = "failed->cli"
    else:
        result = convert(text)
        outcome = "ok" if result is not None else f"{unusable}->cli"
    report.provider, report.model = extraction_llm.provider(), extraction_llm.model()
    report.outcome = outcome
    logger.info(
        "Background model: caller=%s provider=%s model=%s outcome=%s",
        caller,
        report.provider,
        report.model,
        outcome,
    )
    return result


async def _from_cli(
    caller: str,
    prompt: str,
    *,
    max_tokens: int,
    system: str | None,
    role: str = "background",
    timeout: float | None = None,
    requeue: bool = False,
) -> tuple[str | None, str]:
    """One budgeted Grok CLI request: (visible text or None, outcome)."""
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
        logger.warning("Grok CLI %s failed: %s", caller, error)
        return None, "failed"
    except Exception as error:  # a background caller must never see a raise
        logger.warning("Grok CLI %s failed (%s)", caller, type(error).__name__)
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
    if caller in _API_CALLERS:
        api_result, _api_reason = await _from_api(
            caller, prompt, system=system, convert=convert
        )
        if api_result is not None:
            from .xai_spend import CHAT_MODEL

            report.provider, report.model = "xai", CHAT_MODEL
            report.outcome = "ok"
            logger.info(
                "Background model: caller=%s provider=%s model=%s outcome=%s",
                caller,
                report.provider,
                report.model,
                report.outcome,
            )
            return api_result
    budget = haiku_max_tokens or haiku_budget(max_tokens)
    if extraction_llm.uses_anthropic():
        claude_result = await _from_claude(
            caller,
            prompt,
            max_tokens=budget,
            system=system,
            convert=convert,
            unusable=unusable,
            report=report,
            role=role,
            timeout=timeout,
        )
        if claude_result is not None:
            return claude_result
    report.provider, report.model = "grok", extraction_llm.DEFAULT_MODEL
    text, reason = await _from_cli(
        caller,
        prompt,
        max_tokens=budget,
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
