"""One Klukai picture through the xAI image edit API.

Her portrait from the repo goes up as a data URI on every edit. Text-only
generation drops her cap, collar, and puts the tear on the wrong side. The
Commander stays text. The worker turns a grant into the implied scene, so the
flag never arrives; if it did, it is refused before any spend. Failure
falls back to the account CLI. The checker is grok-4.7 vision at low effort,
with the CLI checker if that call fails.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import logging
from pathlib import Path
import time
import urllib.error
import urllib.request

import agy_art_runtime as agy
from agy_image_render import normalize
from fal_image_render import brief
from grok_image_render import _refuse_unsupported, aspect_ratio

logger = logging.getLogger(__name__)
_MAX_RESPONSE = 20_000_000
PORTRAIT = Path(__file__).resolve().parents[1] / "docs" / "images" / "klukai-portrait.png"
# One more try for a passing API failure. The CLI behind it depends on the account's
# balance, so it may not rescue the picture. A slow failure gets no second wait, since
# two of them would outlast the core's deadline.
_RETRY_WITHIN_S = 60.0
_RETRY_PAUSE_S = 2.0


class _PassingFailure(agy.ProviderUnavailable):
    """A rate limit, a server error or a dropped connection: worth one more try."""


def _spend():
    path = Path(__file__).resolve().parents[1] / "docker" / "core" / "app" / "xai_spend.py"
    spec = importlib.util.spec_from_file_location("xai_spend", path)
    if spec is None or spec.loader is None:
        raise agy.RuntimeError("xAI spend ledger is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Billed(agy.RuntimeError):
    """xAI took the request and may have charged it, but no picture came back."""

    def __init__(self, message: str, usd: float) -> None:
        super().__init__(message)
        self.usd = usd


def render_with_fallback(request: dict, references: dict, fallback):
    """API edit when selected and under the cap; otherwise the CLI renderer.

    The edit's price is held in the shared ledger before the request. A
    rejected or unsent request releases it. A billed edit is recorded even
    when its picture is unusable, and is never sent a second time: only a 429
    or 5xx (refused, so not billed) gets one more try. The CLI fallback is
    the account login, never the paid API.
    """
    _refuse_unsupported(request)
    spend = _spend()
    if spend.image_backend() != "api" or not spend.api_key():
        return fallback(request, references)
    try:
        reference = _portrait()
    except OSError as exc:
        logger.warning("xai image portrait unavailable: %s", type(exc).__name__)
        return fallback(request, references)
    allowed, hold = spend.try_reserve("image", spend.image_usd())
    if not allowed:
        return fallback(request, references)
    started = time.monotonic()
    charge = 0.0
    try:
        try:
            image, charge = _edit(request, reference, spend)
        except _PassingFailure:
            if time.monotonic() - started > _RETRY_WITHIN_S:
                raise
            logger.warning("xai image failed in passing; trying once more")
            time.sleep(_RETRY_PAUSE_S)
            image, charge = _edit(request, reference, spend)
    except Exception as exc:
        if isinstance(exc, _Billed):
            charge = exc.usd
        _charge(spend, charge, "image", spend.IMAGE_MODEL, hold)
        logger.warning(
            "xai image falling back to the CLI: %s",
            spend.redact(f"{type(exc).__name__}: {exc}"),
        )
        return fallback(request, references)
    _charge(spend, charge, "image", spend.IMAGE_MODEL, hold)
    return image


def _portrait() -> bytes:
    data = PORTRAIT.read_bytes()
    if not data.startswith(b"\x89PNG") or len(data) > 8_000_000:
        raise OSError("portrait")
    return data


def _charge(spend, usd: float, kind: str, model: str, hold=None) -> None:
    daily = spend.try_settle(hold, "image", usd)
    spend.log_use(kind, model=model, usd=usd, daily_usd=daily)


def _edit(request: dict, reference: bytes, spend) -> tuple[bytes, float]:
    ratio = aspect_ratio(int(request["width"]), int(request["height"]))
    payload = {
        "model": spend.IMAGE_MODEL,
        "prompt": brief(request),
        "images": [
            {"url": "data:image/png;base64," + base64.b64encode(reference).decode("ascii")}
        ],
        "aspect_ratio": ratio,
        "resolution": "2k",
        "response_format": "b64_json",
        "n": 1,
    }
    raw = json.dumps(payload).encode()
    req = urllib.request.Request(
        spend.IMAGE_EDIT_URL,
        data=raw,
        headers={
            "Authorization": "Bearer " + spend.api_key(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            body = response.read(_MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        exc.read(500)
        # The body can echo the prompt. Keep the status only.
        failure = _PassingFailure if exc.code == 429 or exc.code >= 500 else agy.ProviderUnavailable
        raise failure(f"xai image failed: HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # The edit may have finished server-side: count it, never resend it.
        raise _Billed(
            spend.redact(f"xai image is unavailable: {type(exc).__name__}"),
            spend.image_usd(),
        ) from None
    if len(body) > _MAX_RESPONSE:
        raise _Billed("xai image response is too large", spend.image_usd())
    usd = spend.image_usd()
    try:
        data = json.loads(body)
        usage = data.get("usage") if isinstance(data, dict) else None
        exact = spend.ticks_usd(usage.get("cost_in_usd_ticks")) if isinstance(usage, dict) else None
        if exact is not None:
            usd = exact
        encoded = data["data"][0]["b64_json"]
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("image")
        decoded = base64.b64decode(encoded, validate=True)
        return normalize(decoded), usd
    except Exception as exc:
        raise _Billed("xai image response had no usable image", usd) from exc


def check_with_fallback(candidate: bytes, value: dict, references: dict, *, timeout: float, cli):
    """grok-4.7 vision at low effort. The CLI checker runs if this call fails.

    The check's price is held before the request; a billed check that comes
    back unusable is still recorded.
    """
    spend = _spend()
    if spend.image_backend() != "api" or not spend.api_key() or timeout < 5:
        return cli(candidate, value, references, timeout=timeout)
    allowed, hold = spend.try_reserve("image", spend.CHECK_USD)
    if not allowed:
        return cli(candidate, value, references, timeout=timeout)
    billed = {"usd": 0.0}
    try:
        verdict, usd = _check(candidate, value, spend, timeout, billed)
    except Exception as exc:
        _charge(spend, billed["usd"], "check", spend.CHAT_MODEL, hold)
        logger.warning(
            "xai checker falling back to the CLI: %s",
            spend.redact(f"{type(exc).__name__}: {exc}"),
        )
        return cli(candidate, value, references, timeout=timeout)
    _charge(spend, usd, "check", spend.CHAT_MODEL, hold)
    return verdict


def _check(candidate: bytes, value: dict, spend, timeout: float, billed=None) -> tuple[dict, float]:
    billed = {} if billed is None else billed
    from claude_image_check import RUBRIC, expected_people, validate_verdict

    portrait = _portrait()
    who = expected_people(value)
    payload = {
        "model": spend.CHAT_MODEL,
        "reasoning_effort": "low",
        "messages": [
            {"role": "system", "content": RUBRIC},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _data_uri(portrait)}},
                    {"type": "image_url", "image_url": {"url": _data_uri(candidate)}},
                    {
                        "type": "text",
                        "text": (
                            f"Expected people: {who}.\nRequest:\n{value.get('prompt', '')}\n"
                            "Reply with one JSON object: "
                            '{"verdict":"pass"|"reject","issues":[{"severity":"major"|"minor","what":"..."}]}'
                        ),
                    },
                ],
            },
        ],
    }
    req = urllib.request.Request(
        spend.CHAT_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + spend.api_key(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            # From here xAI has answered 200 and billed the check.
            billed["usd"] = spend.CHECK_USD
            body = response.read(2_000_000)
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        # Possibly finished server-side.
        billed["usd"] = spend.CHECK_USD
        raise
    data = json.loads(body)
    usage = data.get("usage") if isinstance(data, dict) else None
    exact = spend.ticks_usd(usage.get("cost_in_usd_ticks")) if isinstance(usage, dict) else None
    if exact is not None:
        billed["usd"] = exact
    text = data["choices"][0]["message"]["content"]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("verdict")
    verdict = validate_verdict(json.loads(text[start : end + 1]))
    verdict["seconds"] = 0.0
    return verdict, exact if exact is not None else spend.CHECK_USD


def _data_uri(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
