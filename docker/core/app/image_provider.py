"""Opt-in, bounded OpenAI image edits using an operator-selected reference pack.

No client, network request, GPU gate or budget reservation is created on import.
The local renderer remains the default; this module never falls back to it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import os
import re
import stat
import threading
import warnings
from pathlib import Path

from .dependencies import http_stream

import httpx
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

OPENAI_IMAGES_ENDPOINT = "https://api.openai.com/v1/images/edits"
DEFAULT_MODEL = "gpt-image-2.5-sunburst-2026-09-08"
ALLOWED_MODELS = {DEFAULT_MODEL, "gpt-image-2.5-flare-2026-09-08"}
MAX_REFERENCE_BYTES = 50_000_000
MAX_REFERENCE_TOTAL_BYTES = 128 * 1024 * 1024
MAX_REFERENCE_PIXELS = 40_000_000
MAX_RESPONSE_BYTES = 48 * 1024 * 1024
REQUEST_DEADLINE_SECONDS = 360
QUEUE_WAIT_SECONDS = 60
_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_cloud_render_lock = asyncio.Semaphore(1)
_reference_lock = threading.Lock()


class ImageProviderError(ValueError):
    """A credential- and prompt-free image validation failure."""


def selected_provider() -> str:
    return os.environ.get("KLUKAI_IMAGE_PROVIDER", "comfyui").strip().lower()


def uses_cloud_images() -> bool:
    return selected_provider() == "openai"


def _settings() -> tuple[str, str, str]:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    model = os.environ.get("KLUKAI_OPENAI_IMAGE_MODEL", DEFAULT_MODEL)
    quality = os.environ.get("KLUKAI_OPENAI_IMAGE_QUALITY", "max")
    if not uses_cloud_images() or not key:
        raise ImageProviderError("Cloud image provider is not configured")
    if model not in ALLOWED_MODELS or quality != "max":
        raise ImageProviderError("Unsupported image model or quality")
    return key, model, quality


def _validate_dimensions(width: int, height: int) -> None:
    if (
        type(width) is not int
        or type(height) is not int
        or min(width, height) <= 0
        or max(width, height) > 3840
        or width % 16
        or height % 16
        or not 655_360 <= width * height <= 8_294_400
        or max(width, height) > 3 * min(width, height)
    ):
        raise ImageProviderError("Unsupported image dimensions")


def _verified_image(raw: bytes, *, output: bool = False) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in _MIME or getattr(image, "n_frames", 1) != 1:
                raise ImageProviderError("Unsupported image format")
            width, height = image.size
            if output:
                _validate_dimensions(width, height)
            elif min(width, height) <= 0 or width * height > MAX_REFERENCE_PIXELS:
                raise ImageProviderError("Reference image exceeds pixel limit")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            # New pixels, no source EXIF/PNG metadata in returned archive images.
            result = Image.new("RGBA" if "A" in image.getbands() else "RGB", image.size)
            result.paste(image.convert(result.mode))
            return result


def _references() -> list[tuple[str, tuple[str, bytes, str]]]:
    # Readiness may run in another worker thread while a render is active.
    # Serialize decoding as well as requests to keep transient PIL memory bounded.
    with _reference_lock:
        return _load_references()


def _load_references() -> list[tuple[str, tuple[str, bytes, str]]]:
    paths = json.loads(os.environ.get("KLUKAI_IMAGE_REFERENCE_FILES", "[]"))
    if not isinstance(paths, list) or not 1 <= len(paths) <= 16:
        raise ImageProviderError("One to sixteen reference files are required")
    files = []
    total = 0
    normalized_total = 0
    for index, value in enumerate(paths):
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ImageProviderError("Reference files must be absolute local paths")
        path = Path(value)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size < MAX_REFERENCE_BYTES:
            raise ImageProviderError(
                "Reference file exceeds byte limit or is not regular"
            )
        # Bound the read even if the file changes after stat().
        with path.open("rb") as handle:
            raw = handle.read(MAX_REFERENCE_BYTES)
        total += len(raw)
        if len(raw) >= MAX_REFERENCE_BYTES or total > MAX_REFERENCE_TOTAL_BYTES:
            raise ImageProviderError("Reference pack exceeds byte limit")
        validated = _verified_image(raw)
        try:
            output = io.BytesIO()
            validated.save(output, format="PNG")
            normalized = output.getvalue()
        finally:
            validated.close()
        normalized_total += len(normalized)
        if (
            len(normalized) >= MAX_REFERENCE_BYTES
            or normalized_total > MAX_REFERENCE_TOTAL_BYTES
        ):
            raise ImageProviderError("Normalized reference pack exceeds byte limit")
        files.append(("image[]", (f"reference-{index}.png", normalized, "image/png")))
    return files


def cloud_images_ready() -> bool:
    """Check configuration without spending or reserving an attempt.

    Remaining caps and writable persistence are enforced atomically at dispatch.
    """
    try:
        _settings()
        for period in ("DAILY", "MONTHLY"):
            if int(os.environ.get(f"KLUKAI_CLOUD_IMAGE_{period}_LIMIT", "0")) <= 0:
                return False
        _references()
        return True
    except (
        ValueError,
        OSError,
        RecursionError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return False


def _plain_description(text: str) -> str:
    text = re.sub(r"<\s*(?:lora|lyco|embedding)\s*:[^>]*>", "", text, flags=re.I)
    text = re.sub(r"\bembedding:[^,\s]+", "", text, flags=re.I)
    text = text.replace(r"\(", "(").replace(r"\)", ")")
    # Peel nested diffusion emphasis while preserving its descriptive contents.
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\(([^()]*)\s*:[+-]?(?:\d+(?:\.\d*)?|\.\d+)\)", r"\1", text)
        text = re.sub(r"\(([^()]*)\)", r"\1", text)
    text = re.sub(r"\[([^\[\]]*)\]", r"\1", text)
    text = text.replace("_", " ")
    text = re.sub(r"\b1girl\b", "one woman", text)
    text = re.sub(r"\b1boy\b", "one man", text)
    text = re.sub(r"\b(\d+)girls\b", r"\1 women", text)
    text = re.sub(r"\b(\d+)boys\b", r"\1 men", text)
    tags = [tag.strip() for tag in text.split(",")]
    tags = [
        tag
        for tag in tags
        if tag
        and tag.lower() not in {"masterpiece", "best quality", "absurdres", "highres"}
        and not re.fullmatch(r"score \d+(?: up)?", tag, flags=re.I)
    ]
    return "; ".join(tags)


# Owner decision 2026-10-19: tasteful nudity of Klukai, alone, past the intimacy
# gate, when the scene itself asks for it. Only a renderer whose rules allow it
# (Grok, in the account worker) receives that permission; every other renderer
# is told to keep her covered. Whole words; "bed" alone is not a cue, nor is a
# meteor shower or a bathing suit.
_NUDITY_CUE = re.compile(
    r"\b(nude|nudity|naked|undress(?:ed|ing)?|topless|skinny[- ]dip(?:ping)?|"
    r"bath|bathing(?! suits?\b)|bathtub|onsen|hot springs?|(?<!meteor )shower(?:ing)?)\b",
    re.IGNORECASE,
)


def nudity_allowed(
    text: str | None, *, intimate: bool, include_commander: bool
) -> bool:
    """True only past the intimacy gate, for Klukai alone, when the scene asks."""
    return bool(
        intimate
        and not include_commander
        and isinstance(text, str)
        and _NUDITY_CUE.search(text)
    )


def cloud_prompt(
    prompt: str, *, sfw: bool = False, negative_extra: str | None = None
) -> str:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32_000:
        raise ImageProviderError("Invalid image description")
    parts = [
        "Create one polished anime illustration of the described scene. "
        "Use the supplied character references to preserve each character's face, "
        "hair, eyes and distinctive markings. Follow the described clothing and "
        "pose; keep different characters visually distinct.",
        "Scene and appearance: " + _plain_description(prompt) + ".",
    ]
    if sfw:
        parts.append(
            "Keep every character fully clothed, with no nudity or exposed intimate anatomy."
        )
    if negative_extra:
        if not isinstance(negative_extra, str) or len(negative_extra) > 4000:
            raise ImageProviderError("Invalid image exclusions")
        parts.append(
            "Avoid these visual elements: " + _plain_description(negative_extra) + "."
        )
    description = "\n".join(parts)
    if len(description) > 32_000:
        raise ImageProviderError("Image description exceeds API limit")
    return description


async def generate_cloud_image(
    prompt: str,
    width: int,
    height: int,
    *,
    sfw: bool = False,
    negative_extra: str | None = None,
) -> bytes | None:
    """Make exactly one edit request; budget failures propagate to batch callers."""
    try:
        await asyncio.wait_for(_cloud_render_lock.acquire(), timeout=QUEUE_WAIT_SECONDS)
    except TimeoutError:
        logger.info("Cloud image renderer busy; no attempt reserved")
        return None
    try:
        return await _generate_cloud_image(
            prompt, width, height, sfw=sfw, negative_extra=negative_extra
        )
    finally:
        _cloud_render_lock.release()


async def _generate_cloud_image(
    prompt: str,
    width: int,
    height: int,
    *,
    sfw: bool,
    negative_extra: str | None,
) -> bytes | None:
    try:
        key, model, quality = _settings()
        _validate_dimensions(width, height)
        description = cloud_prompt(prompt, sfw=sfw, negative_extra=negative_extra)
        references = await asyncio.to_thread(_references)
    except (
        ValueError,
        OSError,
        RecursionError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        logger.warning("Cloud image configuration rejected (%s)", type(exc).__name__)
        return None

    from .creative_budget import reserve_attempt, record_usage

    attempt_id = await reserve_attempt("image")
    try:
        async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    REQUEST_DEADLINE_SECONDS, connect=10, write=30, pool=10
                ),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                async with http_stream(
                    "cloud_images",
                    client,
                    "POST",
                    OPENAI_IMAGES_ENDPOINT,
                    headers={"Authorization": f"Bearer {key}"},
                    data={
                        "model": model,
                        "quality": quality,
                        "size": f"{width}x{height}",
                        "n": "1",
                        "prompt": description,
                        "output_format": "png",
                    },
                    files=references,
                ) as response:
                    if response.status_code != 200:
                        logger.warning(
                            "Cloud image request failed (HTTP %d, attempt %s)",
                            response.status_code,
                            attempt_id,
                        )
                        return None
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ImageProviderError(
                                "Image response exceeds byte limit"
                            )
                        raw.extend(chunk)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ImageProviderError("Malformed image response")
        if isinstance(payload.get("usage"), dict):
            try:
                await record_usage(attempt_id, payload["usage"])
            except Exception as exc:
                logger.warning(
                    "Cloud image usage recording failed (%s)", type(exc).__name__
                )
        data = payload.get("data")
        if (
            not isinstance(data, list)
            or len(data) != 1
            or not isinstance(data[0], dict)
        ):
            raise ImageProviderError("Expected one generated image")
        encoded = data[0].get("b64_json")
        if not isinstance(encoded, str):
            raise ImageProviderError("Expected base64 image output")
        decoded = base64.b64decode(encoded, validate=True)
        image = await asyncio.to_thread(_verified_image, decoded, output=True)
        try:
            if image.size != (width, height):
                raise ImageProviderError(
                    "Generated image dimensions do not match request"
                )
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
        finally:
            image.close()
    except (
        httpx.HTTPError,
        TimeoutError,
        ValueError,
        OSError,
        RecursionError,
        binascii.Error,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        logger.warning(
            "Cloud image request rejected (%s, attempt %s)",
            type(exc).__name__,
            attempt_id,
        )
        return None
