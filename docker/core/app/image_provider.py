"""Opt-in, bounded OpenAI image edits using an operator-selected reference pack.

No client, network request or budget reservation is created on import.
The account worker is the default renderer; this provider is the explicit
``KLUKAI_IMAGE_PROVIDER=openai`` option. Pictures are cloud-only: there is no
local renderer to fall back to.
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


def render_lock_held() -> bool:
    """True while this process holds the single cloud-edit slot."""
    return _cloud_render_lock.locked()


class ImageProviderError(ValueError):
    """A credential- and prompt-free image validation failure."""


def selected_provider() -> str:
    """The scene renderer. Unset means the account worker ("agy"); "openai"
    is the opt-in edit provider. Any other value disables generation."""
    return os.environ.get("KLUKAI_IMAGE_PROVIDER", "agy").strip().lower()


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


# Image models paint a name they are given as lettering. Every path to the image
# model passes through _plain_description, including words he or she typed.
# Diffusion words with no place in a prompt for a 2D anime render. The render terms pull
# towards the photorealistic or 3D look the checker rejects, and a body-shape word from a
# squad entry must not make anyone read as young. "Palette, film grain, and styling come
# after" the subject (xAI's prompting guide).
_NOT_FOR_THE_MODEL = frozenset(
    {
        "masterpiece", "best quality", "absurdres", "highres", "very aesthetic",
        "ultra-detailed", "beautiful lighting", "depth of field", "sharp focus",
        "cinematic composition", "vivid colors", "professional", "high resolution",
        "intricate details", "ambient occlusion", "volumetric lighting", "film grain",
        "detailed skin", "perfect anatomy", "detailed face", "beautiful detailed eyes",
        "expressive eyes", "petite", "small build", "short stature", "seductive",
        "seductive pose", "busty", "large breasts", "garter straps",
    }
)  # fmt: skip
_ELMO_NAME = re.compile(r"\b(?:the\s+)?elmo(?:(?P<possessive>['’]s))?\b", re.I)


def _unnamed_base(match: re.Match[str]) -> str:
    return "the mobile base's" if match["possessive"] else "the mobile base"


def _plain_description(text: str) -> str:
    text = _ELMO_NAME.sub(_unnamed_base, text)
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
    kept: dict[str, str] = {}
    for tag in (tag.strip() for tag in text.split(",")):
        low = tag.lower()
        if (
            tag
            and low not in _NOT_FOR_THE_MODEL
            and not re.fullmatch(r"score \d+(?: up)?", tag, flags=re.I)
        ):
            kept.setdefault(low, tag)
    return "; ".join(kept.values())


# "draw it" names nothing; picture_intent's retry marker is for the journal only.
_BARE_REFERENCE = re.compile(
    r"^\W*(?:please\s+|can you\s+|could you\s+|would you\s+|will you\s+)*"
    r"(?:draw|sketch|paint|render|illustrate|doodle|show me)\s+(?:it|that|this|one|something)"
    r"(?:\s+(?:for me|for us|please))*\W*$",
    re.I,
)
_RETRY_MARKER = re.compile(r"\n\nNew version \d+\.\s*$")
_SUBJECT_LINE = re.compile(r"\n\nSubject: (.+)")


def plain_request(text: str) -> str:
    """His request as the image model reads it: what to show, not the order to draw it."""
    text = _RETRY_MARKER.sub("", text)
    subject = _SUBJECT_LINE.search(text)
    if subject:
        text = subject.group(1)
    elif _BARE_REFERENCE.match(text):
        return ""
    return _ELMO_NAME.sub(_unnamed_base, text).strip()


# Owner decision 2026-10-19: tasteful nudity of Klukai, alone, past the intimacy
# gate, when the scene itself asks for it. Only a renderer whose rules allow it
# (Grok, in the account worker) receives that permission; every other renderer
# is told to keep her covered. Whole words; "bed" alone is not a cue, nor is a
# meteor shower or a bathing suit.
_NUDITY_CUE = re.compile(
    r"\b(nude|nudity|naked|undress(?:ed|ing)?|topless|skinny[- ]dip(?:ping)?|"
    r"bath|bathing(?! suits?\b)|bathtub|onsen|hot springs?|"
    r"(?<!meteor )(?<!rain )(?<!baby )(?<!bridal )shower(?:ing)?)\b",
    re.IGNORECASE,
)


# "Not naked", "no nudity", "without undressing": a refusal, never a request.
_NUDITY_REFUSED = re.compile(
    r"\b(?:not|no|never|without|don'?t)\s+(?:\w+\s+){0,2}$", re.IGNORECASE
)


_BATHING = re.compile(
    r"\b(?:bath|bathing|bathtub|onsen|hot springs?|shower(?:ing)?)\b", re.I
)


def implied_cover(request: str) -> str:
    """What covers her when a grant becomes an implied picture."""
    if _BATHING.search(request):
        return "large white towel wrapped around her, hair down, bare shoulders"
    return "soft white bedsheet wrapped around her, hair down, bare shoulders"


def _not_alone(text: str) -> bool:
    """Another girl is named or tagged ("Groza", "2girls", "multiple girls")."""
    from .image_gen_constants import SQUAD_KEYWORDS

    names = "|".join(re.escape(name) for name in SQUAD_KEYWORDS)
    return bool(
        re.search(
            rf"\b(?:{names})\b|\b(?:[2-9]|\d\d+)\s*girls\b|\bmultiple girls\b",
            text,
            re.IGNORECASE,
        )
    )


def asks_for_nudity(text: str) -> bool:
    asked = False
    for match in _NUDITY_CUE.finditer(text):
        if _NUDITY_REFUSED.search(text[max(0, match.start() - 60) : match.start()]):
            return False
        asked = True
    return asked


_INTIMATE_WORDS = re.compile(
    r"\b(?:lingerie|underwear|bras?|panties|bikinis?|swimsuits?|towels?|beds?|sexy|lewd|"
    r"kiss(?:es|ed|ing)?|cuddl(?:e|es|ed|ing)|make out|making out)\b",
    re.I,
)


def intimate_with_others(text: str) -> bool:
    """Another character is named alongside anything intimate: nothing is drawn."""
    return _not_alone(text) and bool(
        _NUDITY_CUE.search(text) or _INTIMATE_WORDS.search(text)
    )


def nudity_allowed(
    text: str | None, *, intimate: bool, include_commander: bool
) -> bool:
    """True only past the intimacy gate, for Klukai alone, when the scene asks."""
    return bool(
        intimate
        and not include_commander
        and isinstance(text, str)
        and not _not_alone(text)
        and asks_for_nudity(text)
    )


def cloud_prompt(
    prompt: str, *, sfw: bool = False, negative_extra: str | None = None
) -> str:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32_000:
        raise ImageProviderError("Invalid image description")
    parts = [
        "One polished anime illustration of the scene below, in its clothing and pose; "
        "any other characters stay visually distinct.",
        "Scene and appearance: " + _plain_description(prompt) + ".",
    ]
    if sfw:
        parts.append(
            "Keep every character fully clothed in the outfit listed (swimwear or a leotard "
            "counts as clothed), with no nudity or exposed intimate anatomy."
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
