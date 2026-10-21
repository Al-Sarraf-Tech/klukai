"""Account images through the isolated Agy/Grok host worker.

Only a private bearer token crosses the Unix socket. Desktop credentials,
Google API clients, retries and alternate renderers do not belong here.
"""

from __future__ import annotations
from typing import Any


import asyncio
import io
import json
import logging
import os
from pathlib import Path
import stat
import uuid
import warnings

from .dependencies import http_stream

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
REQUEST_DEADLINE_SECONDS = 380  # Two bounded 170s providers plus transport/setup.


def uses_agy_images(user_id: str | None) -> bool:
    """Only authenticated allowlisted identities may use the selected worker."""
    from .image_provider import selected_provider

    allowed = {
        user.strip()
        for user in os.environ.get("KLUKAI_AGY_ALLOWED_USERS", "").split(",")
        if user.strip()
    }
    return (
        (
            os.environ.get("KLUKAI_CHAT_IMAGE_PROVIDER", "").strip().lower() == "agy"
            # Unset means the account worker, exactly as selected_provider() reads it.
            or selected_provider() == "agy"
        )
        and bool(user_id)
        and user_id in allowed
    )


def _settings() -> tuple[str, str]:
    socket = Path(os.environ.get("KLUKAI_AGY_SOCKET", "/run/klukai-agy/image.sock"))
    token_path = Path(os.environ.get("KLUKAI_AGY_TOKEN_FILE", "/run/klukai-agy/token"))
    if not socket.is_absolute() or not token_path.is_absolute():
        raise ValueError("Agy worker paths must be absolute")
    with token_path.open("rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Agy worker token must be private")
        token = handle.read(4097).decode("ascii").strip()
    if (
        not token
        or len(token) > 4096
        or any(character.isspace() for character in token)
    ):
        raise ValueError("Invalid Agy worker token")
    return str(socket), token


def _normalize_png(raw: bytes) -> bytes:
    """Fully validate bounded pixels and remove all source metadata."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if (
                image.format != "PNG"
                or getattr(image, "n_frames", 1) != 1
                or min(width, height) <= 0
                or width * height > MAX_IMAGE_PIXELS
            ):
                raise ValueError("Unsupported Agy image")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            mode = (
                "RGBA"
                if "A" in image.getbands() or "transparency" in image.info
                else "RGB"
            )
            with Image.new(mode, image.size) as clean:
                clean.paste(image.convert(mode))
                output = io.BytesIO()
                clean.save(output, format="PNG")
    normalized = output.getvalue()
    if len(normalized) > MAX_IMAGE_BYTES:
        raise ValueError("Agy image exceeds byte limit")
    return normalized


async def agy_images_ready() -> bool:
    """Check worker liveness/configuration, without asserting Google login."""
    try:
        socket, token = _settings()
        async with asyncio.timeout(5):
            transport = httpx.AsyncHTTPTransport(uds=socket, retries=0, trust_env=False)
            async with httpx.AsyncClient(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(5),
            ) as client:
                async with http_stream(
                    "account_images",
                    client,
                    "GET",
                    "http://klukai-agy/health",
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if response.status_code != 200:
                        return False
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > 8192:
                            return False
                        raw.extend(chunk)
        data = json.loads(raw)
        return (
            isinstance(data, dict)
            and data.get("status") == "ready"
            and data.get("provider") == "agy"
            and data.get("busy") is False
        )
    except (OSError, ValueError, httpx.HTTPError, TimeoutError):
        return False


def _request_id(request_id: str | None) -> str:
    if request_id is None:
        return str(uuid.uuid4())
    parsed = uuid.UUID(str(request_id))
    if str(parsed) != str(request_id).lower():
        raise ValueError("Invalid Agy image request")
    return str(parsed)


def _interpret_worker(
    path: str, status: int, body: bytes, content_type: str
) -> dict[str, Any]:
    """Map one worker response. Recovery never treats a miss as a new render."""
    if status == 200 and content_type.split(";", 1)[0].strip().lower() == "image/png":
        return {"status": "completed", "image": _normalize_png(body)}
    if path == "/recover":
        try:
            data = json.loads(body) if body else {}
        except (UnicodeError, json.JSONDecodeError):
            data = {}
        reported = data.get("status") if isinstance(data, dict) else None
        if reported in {
            "unknown",
            "pending",
            "refused",
            "failed",
            "conflict",
            "unavailable",
        }:
            return {"status": reported, "image": None}
        if status == 404:
            return {"status": "unknown", "image": None}
        if status == 403:
            return {"status": "refused", "image": None}
        return {"status": "unavailable", "image": None}
    if status == 403:
        return {"status": "refused", "image": None}
    if status == 429:
        # Worker checks its slot before inserting a claim. This is the only
        # response that proves this request was not accepted for generation.
        return {"status": "busy", "image": None}
    if status == 409:
        return {"status": "pending", "image": None}
    logger.warning("Agy image worker rejected request (HTTP %d)", status)
    return {"status": "failed", "image": None}


async def _post_worker(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    socket, token = _settings()
    async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
        transport = httpx.AsyncHTTPTransport(uds=socket, retries=0, trust_env=False)
        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(REQUEST_DEADLINE_SECONDS, connect=5),
        ) as client:
            async with http_stream(
                "account_images",
                client,
                "POST",
                f"http://klukai-agy{path}",
                json=payload,
                headers={"Authorization": f"Bearer {token}", "Accept": "image/png"},
            ) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_IMAGE_BYTES:
                        raise ValueError("Agy image exceeds byte limit")
                    raw.extend(chunk)
                content_type = response.headers.get("content-type", "")
                status = response.status_code
    return _interpret_worker(path, status, bytes(raw), content_type)


async def _validated_payload(
    prompt: str,
    *,
    user_id: str,
    width: int,
    height: int,
    include_commander: bool,
    request_id: str | None,
    nudity: bool = False,
) -> dict[str, Any] | None:
    if not uses_agy_images(user_id):
        return None
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or len(prompt) > 16_000
        or type(width) is not int
        or type(height) is not int
        or min(width, height) <= 0
        or width * height > MAX_IMAGE_PIXELS
        or type(include_commander) is not bool
        or type(nudity) is not bool
        or (nudity and include_commander)
    ):
        raise ValueError("Invalid Agy image request")
    payload: dict[str, Any] = {
        "request_id": _request_id(request_id),
        "user_id": user_id,
        "prompt": prompt,
        "width": width,
        "height": height,
        "include_commander": include_commander,
    }
    if nudity:
        # Present only when granted, so every earlier request keeps its digest.
        payload["nudity"] = True
    return payload


async def submit_agy_image(
    prompt: str,
    *,
    user_id: str,
    width: int = 832,
    height: int = 1216,
    include_commander: bool = False,
    request_id: str | None = None,
    nudity: bool = False,
) -> dict[str, Any]:
    """POST /generate once. A miss is not retried and never falls back."""
    try:
        payload = await _validated_payload(
            prompt,
            user_id=user_id,
            width=width,
            height=height,
            include_commander=include_commander,
            request_id=request_id,
            nudity=nudity,
        )
        if payload is None:
            return {"status": "refused", "image": None}
        return await _post_worker("/generate", payload)
    except (
        OSError,
        ValueError,
        SyntaxError,
        httpx.HTTPError,
        TimeoutError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        logger.warning("Agy image request failed (%s)", type(error).__name__)
        return {"status": "unknown", "image": None}


async def recover_agy_image(
    prompt: str,
    *,
    user_id: str,
    width: int,
    height: int,
    include_commander: bool,
    request_id: str,
    nudity: bool = False,
) -> dict[str, Any]:
    """POST /recover only. This never submits, rotates, or renders."""
    try:
        payload = await _validated_payload(
            prompt,
            user_id=user_id,
            width=width,
            height=height,
            include_commander=include_commander,
            request_id=request_id,
            nudity=nudity,
        )
        if payload is None:
            return {"status": "refused", "image": None}
        return await _post_worker("/recover", payload)
    except (
        OSError,
        ValueError,
        SyntaxError,
        httpx.HTTPError,
        TimeoutError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        logger.warning("Agy image recovery failed (%s)", type(error).__name__)
        return {"status": "unknown", "image": None}


async def generate_agy_image(
    prompt: str,
    *,
    user_id: str,
    width: int = 832,
    height: int = 1216,
    include_commander: bool = False,
    request_id: str | None = None,
    nudity: bool = False,
) -> bytes | None:
    """Submit exactly one request; failures are generic and never fall back."""
    result = await submit_agy_image(
        prompt,
        user_id=user_id,
        width=width,
        height=height,
        include_commander=include_commander,
        request_id=request_id,
        nudity=nudity,
    )
    if result.get("status") == "completed":
        return result.get("image")
    return None
