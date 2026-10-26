"""Optional, separately capped reference edits; no credentials or calls at import.

The worker selects these adapters explicitly and owns demand-only initiation,
request deduplication, and Claude approval. Missing provider caps disable an
adapter before private reads. Every dispatch reserves one durable attempt;
there are no submission retries, including after an uncertain transport failure.

Verified 2026-10-26:
https://developers.openai.com/api/reference/resources/images/methods/edit
https://ai.google.dev/api/interactions-api.md
https://ai.google.dev/gemini-api/docs/image-generation
"""

from __future__ import annotations

import base64
from contextlib import closing
from datetime import UTC, datetime
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid

from PIL import Image

import agy_art_runtime as agy
from agy_image_render import MAX_BYTES, normalize
from fal_image_render import brief
from gemini_art_review import read_image

ENDPOINTS = {
    "openai": "https://api.openai.com/v1/images/edits",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/interactions",
}
OPENAI_MODEL = "gpt-image-2.5-sunburst-2026-09-08"
GEMINI_MODEL = "gemini-nano-banana-2.1"
TIMEOUT_SECONDS = 150
MAX_RESPONSE_BYTES = MAX_BYTES * 2
MAX_REFERENCE_BYTES = 12 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
STATE_DIRECTORY = Path.home() / ".local/share/klukai/agy-bridge/state"
ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
REFUSAL_CODES = frozenset({
    "moderation_blocked", "safety", "recitation", "language", "prohibited_content",
    "spii", "blocklist", "image_safety", "image_prohibited_content", "image_recitation",
    "image_other", "content_blocked",
})


class SubmissionUncertain(agy.GenerationPending):
    """A paid request may have run; never treat this as permission to fail over."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urlopen(request, timeout):
    # Credentials only go to the fixed HTTPS endpoints. Ignore proxy environment
    # variables and never forward a key to a redirect target.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, timeout=timeout)


def _secret_store():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops/amarillo/scripts"))
    import klukai_secrets

    return klukai_secrets


def _key(provider: str) -> str:
    try:
        raw = _secret_store().decrypt("compose-env").decode()
        expected = provider.upper() + "_API_KEY"
        for line in raw.splitlines():
            name, _, value = line.partition("=")
            if name.strip() == expected:
                value = value.strip().strip("\"'")
                if value and value.isascii() and all(32 < ord(char) < 127 for char in value):
                    return value
                break
    except Exception:
        # Credential-wrapper exceptions may contain private paths or payloads.
        pass
    raise agy.ProviderUnavailable(f"{provider} image key unavailable") from None


def _limits(provider: str) -> tuple[int, int]:
    if provider not in ENDPOINTS:
        raise agy.RuntimeError("Unsupported image provider")
    try:
        values = tuple(int(os.environ.get(f"KLUKAI_{provider.upper()}_IMAGE_{period}_LIMIT", "0"))
                       for period in ("DAILY", "MONTHLY"))
        if min(values) > 0:
            return values
    except ValueError:
        pass
    raise agy.ProviderUnavailable(f"{provider} image allowance not configured") from None


def reserve_attempt(provider: str, request_id: str) -> None:
    """Atomic UTC caps, independent per provider, never refunded after dispatch."""
    daily, monthly = _limits(provider)
    path = Path(os.environ.get(f"KLUKAI_{provider.upper()}_IMAGE_LEDGER",
                               str(STATE_DIRECTORY / f"{provider}-image-attempts.sqlite3")))
    now = datetime.now(UTC)
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise OSError("ledger")
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        with closing(sqlite3.connect(path, timeout=5.0, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS image_attempts (provider TEXT NOT NULL, "
                       "request_id TEXT NOT NULL, day TEXT NOT NULL, month TEXT NOT NULL, reserved_at TEXT NOT NULL)")
            in_month, in_day = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(day = ?), 0) FROM image_attempts WHERE provider = ? AND month = ?",
                (day, provider, month),
            ).fetchone()
            if in_day >= daily or in_month >= monthly:
                db.execute("ROLLBACK")
                raise agy.ProviderUnavailable(f"{provider} image allowance exhausted")
            db.execute("INSERT INTO image_attempts VALUES (?, ?, ?, ?, ?)",
                       (provider, request_id, day, month, now.isoformat()))
            db.execute("COMMIT")
    except (OSError, sqlite3.Error):
        raise agy.ProviderUnavailable(f"{provider} image allowance ledger unavailable") from None


def _validate(request: dict) -> None:
    if not isinstance(request, dict):
        raise agy.RuntimeError("Invalid image request")
    if request.get("nudity") is not None and request.get("nudity") is not False:
        raise agy.RuntimeError("Nudity rendering is unsupported; provider safety checks stay enabled")
    if type(request.get("include_commander")) is not bool:
        raise agy.RuntimeError("Invalid image participants")
    if request["include_commander"] and request.get("user_id") != "jalsarraf":
        raise agy.RuntimeError("Commander reference is not authorized")
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000 or "\x00" in prompt:
        raise agy.RuntimeError("Invalid image description")
    try:
        if str(uuid.UUID(request["request_id"])) != request["request_id"]:
            raise ValueError("request identity")
    except (ValueError, TypeError, KeyError, AttributeError):
        raise agy.RuntimeError("Invalid image request identity") from None
    width, height = request.get("width"), request.get("height")
    if (type(width) is not int or type(height) is not int or min(width, height) <= 0
            or max(width, height) > 3840 or width % 16 or height % 16
            or not 655_360 <= width * height <= 8_294_400
            or max(width, height) > 3 * min(width, height)):
        raise agy.RuntimeError("Unsupported image dimensions")


def _aspect(width: int, height: int) -> str:
    def distance(ratio):
        numerator, denominator = map(int, ratio.split(":"))
        return abs(math.log((width / height) / (numerator / denominator)))

    return min(ASPECT_RATIOS, key=distance)


def _body(provider: str, request: dict, reference: bytes) -> dict:
    encoded = base64.b64encode(reference).decode("ascii")
    prompt = brief(request)
    if provider == "openai":
        # The current edit endpoint supports JSON data-URI image references.
        return {"model": OPENAI_MODEL, "prompt": prompt,
                "images": [{"image_url": "data:image/png;base64," + encoded}],
                "n": 1, "quality": "max", "size": f'{request["width"]}x{request["height"]}',
                "output_format": "png", "moderation": "auto"}
    return {"model": GEMINI_MODEL,
            "input": [{"type": "text", "text": prompt},
                      {"type": "image", "mime_type": "image/png", "data": encoded}],
            "response_format": {"type": "image", "delivery": "inline",
                                "aspect_ratio": _aspect(request["width"], request["height"]), "image_size": "2K"},
            "store": False, "background": False, "stream": False}


def _error_code(document) -> str | None:
    error = document.get("error") if isinstance(document, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _post(provider: str, body: dict, key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    headers.update({"Authorization": f"Bearer {key}"} if provider == "openai" else {"x-goog-api-key": key})
    request = urllib.request.Request(ENDPOINTS[provider], data=json.dumps(body).encode(),
                                     method="POST", headers=headers)
    try:
        with _urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise agy.RuntimeError(f"{provider} image response exceeds byte limit")
        result = json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            with closing(exc):
                code = _error_code(json.loads(exc.read(MAX_ERROR_BYTES)))
        except (ValueError, OSError, RecursionError, http.client.HTTPException):
            code = None
        if code in REFUSAL_CODES:
            raise agy.ContentRefused(f"{provider} refused the image content") from None
        if code == "no_image":
            raise agy.RuntimeError(f"{provider} did not generate an image") from None
        if exc.code in (401, 402, 403, 429):
            raise agy.ProviderUnavailable(f"{provider} image service unavailable (HTTP {exc.code})") from None
        if exc.code == 408 or exc.code >= 500:
            raise SubmissionUncertain(f"{provider} image submission outcome is uncertain") from None
        raise agy.RuntimeError(f"{provider} rejected the image request (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise SubmissionUncertain(f"{provider} image submission outcome is uncertain") from None
    except (ValueError, RecursionError):
        raise agy.RuntimeError(f"{provider} returned an invalid image response") from None
    if not isinstance(result, dict):
        raise agy.RuntimeError(f"{provider} returned an invalid image response")
    if _error_code(result) in REFUSAL_CODES:
        raise agy.ContentRefused(f"{provider} refused the image content")
    if "error" in result:
        raise agy.RuntimeError(f"{provider} returned an image generation error")
    return result


def _image(provider: str, result: dict, reference: bytes, request: dict) -> bytes:
    if provider == "openai":
        images = result.get("data")
        encoded = images[0].get("b64_json") if isinstance(images, list) and len(images) == 1 and isinstance(images[0], dict) else None
    else:
        if result.get("status") != "completed" or not isinstance(result.get("steps"), list):
            raise agy.RuntimeError("gemini did not complete the image")
        images = []
        for step in result["steps"]:
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            content = step.get("content")
            if not isinstance(content, list):
                raise agy.RuntimeError("gemini returned invalid image content")
            images.extend(item for item in content if isinstance(item, dict) and item.get("type") == "image")
        encoded = images[0].get("data") if len(images) == 1 and not images[0].get("uri") else None
    if not isinstance(encoded, str) or not 0 < len(encoded) <= (MAX_BYTES + 2) // 3 * 4:
        raise agy.RuntimeError(f"{provider} did not return one inline image")
    try:
        image = normalize(base64.b64decode(encoded, validate=True))
        if hashlib.sha256(image).digest() == hashlib.sha256(normalize(reference)).digest():
            raise agy.RuntimeError("Image provider returned the reference instead of a new image")
        with Image.open(io.BytesIO(image)) as decoded:
            width, height = decoded.size
        if provider == "openai":
            valid_dimensions = (width, height) == (request["width"], request["height"])
        else:
            numerator, denominator = map(int, _aspect(request["width"], request["height"]).split(":"))
            valid_dimensions = (min(width, height) >= 512 and width * height >= 655_360
                                and abs((width / height) / (numerator / denominator) - 1) <= 0.03)
        if not valid_dimensions:
            raise agy.RuntimeError("Generated image dimensions do not match the request")
        return image
    except (ValueError, OSError):
        raise agy.RuntimeError(f"{provider} returned an unusable image") from None


def _render(provider: str, request: dict, references: dict) -> bytes:
    _validate(request)
    _limits(provider)  # Disabled providers never read references or credentials.
    try:
        reference = read_image(references["klukai"])
        # Compressed inputs can expand when the manifest reader normalizes PNG.
        # Stay below both providers' inline request limits after base64 expansion.
        if not 0 < len(reference) <= MAX_REFERENCE_BYTES:
            raise ValueError("normalized reference limit")
    except (ValueError, KeyError, TypeError, OSError):
        raise agy.RuntimeError("Approved Klukai image reference is unavailable") from None
    body = _body(provider, request, reference)
    key = _key(provider)
    reserve_attempt(provider, request["request_id"])
    return _image(provider, _post(provider, body, key), reference, request)


def render_openai(request: dict, references: dict) -> bytes:
    return _render("openai", request, references)


def render_gemini(request: dict, references: dict) -> bytes:
    return _render("gemini", request, references)
