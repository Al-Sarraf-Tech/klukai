"""Have her ready before the Commander finishes typing.

LM Studio is load-on-demand with a hard 15-minute residency ceiling, which is
deliberate — the GPU is shared with image generation and the policy is not to
hold VRAM around the clock. The cost is that the first message after a quiet
spell pays a cold model load, and he sits watching a loading bar when what he
wanted was her.

This closes that gap without touching the residency policy. The moment he opens
the app the socket connects, and that connect is the signal: start loading the
model *then*, in the background, so it finishes while he is still typing. By the
time he hits send she is already there.

Deliberately NOT keepalive. ``LLMRouter.keepalive`` is a policy-enforced no-op
and must stay one — this holds no VRAM, it only moves an unavoidable load
earlier, into time he was going to spend typing anyway.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

# A reconnect storm (flaky wifi, a tab reopened repeatedly) must not turn into a
# burst of load requests at the GPU.
_MIN_INTERVAL_SECONDS = 60.0
_last_attempt: float = 0.0
_lock = asyncio.Lock()

# The warm request must be the smallest thing that still forces residency.
_WARM_MAX_TOKENS = 1
_WARM_TIMEOUT = 120.0
_STATE_TIMEOUT = 8.0


def _reset_for_tests() -> None:
    global _last_attempt
    _last_attempt = 0.0


async def is_loaded(model: str) -> bool | None:
    """Whether ``model`` is currently resident.

    None means "could not tell" — the gateway is unreachable or does not expose
    model state. Callers treat that as "do not attempt", because a warm request
    to a gateway that is down is pure cost.
    """
    import aiohttp

    from .lm_gateway import lm_studio_auth_headers
    from .llm_router import LM_STUDIO_URL

    try:
        async with aiohttp.ClientSession(headers=lm_studio_auth_headers()) as s:
            async with s.get(
                f"{LM_STUDIO_URL}/api/v0/models",
                timeout=aiohttp.ClientTimeout(total=_STATE_TIMEOUT),
            ) as r:
                if r.status != 200:
                    return None
                body = await r.json()
    except Exception as e:
        logger.debug("Could not read model state: %s", e)
        return None

    for entry in (body or {}).get("data", []):
        if entry.get("id") == model:
            state = str(entry.get("state") or "")
            return state == "loaded"
    return None


async def warm_chat_model(force: bool = False) -> bool:
    """Bring the chat model into residency if it is not already there.

    Returns True only when a load was actually issued. Never raises: a failed
    warm-up costs him a slower first message, which is exactly what would have
    happened anyway, so it must never surface as an error.
    """
    global _last_attempt

    if os.environ.get("KLUKAI_DISABLE_WARMUP"):
        return False

    async with _lock:
        now = time.monotonic()
        if not force and _last_attempt and (now - _last_attempt) < _MIN_INTERVAL_SECONDS:
            return False
        _last_attempt = now

    from .llm_router import LOCAL_CASUAL

    loaded = await is_loaded(LOCAL_CASUAL)
    if loaded is not False:
        # Already resident, or the gateway could not tell us. Either way there
        # is nothing useful to do.
        return False

    return await _issue_warm_request(LOCAL_CASUAL)


async def _issue_warm_request(model: str) -> bool:
    """Force residency with the smallest possible completion."""
    import aiohttp

    from .lm_gateway import LM_TTL_SECONDS, lm_studio_auth_headers
    from .llm_router import LM_STUDIO_URL, get_lm_gate, mark_model_used

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "."}],
        "max_tokens": _WARM_MAX_TOKENS,
        "temperature": 0.0,
        "stream": False,
        # Same residency ceiling as every other request — warming must not be a
        # backdoor that extends how long the model stays in VRAM.
        "ttl": LM_TTL_SECONDS,
    }

    started = time.monotonic()
    try:
        # Through the shared gate so a warm-up never races a real reply or an
        # image render for the GPU.
        async with get_lm_gate():
            async with aiohttp.ClientSession(headers=lm_studio_auth_headers()) as s:
                async with s.post(
                    f"{LM_STUDIO_URL}/v1/chat/completions",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=_WARM_TIMEOUT),
                ) as r:
                    await r.read()
                    ok = r.status == 200
    except Exception as e:
        logger.info("Warm-up did not complete (%s) — first reply may be slow", e)
        return False

    elapsed = time.monotonic() - started
    if ok:
        mark_model_used(model)
        logger.info("Chat model warmed in %.1fs (%s)", elapsed, model)
    else:
        logger.info("Warm-up rejected after %.1fs", elapsed)
    return ok


def warm_in_background() -> asyncio.Task | None:
    """Fire-and-forget warm-up. Never blocks the caller, never raises."""
    try:
        return asyncio.create_task(warm_chat_model())
    except Exception as e:
        logger.debug("Could not schedule warm-up: %s", e)
        return None
