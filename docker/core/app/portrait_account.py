"""Live Portrait on the account worker: one base frame per outfit.

The retired local renderer drew every portrait set (a base plus five masked
inpaints for blink, talk, smile, blush and annoyed). Pictures are cloud-only
(owner decisions 2026-10-19 and 2026-10-26), and the account worker
(``KLUKAI_IMAGE_PROVIDER=agy``) has no inpaint. An outfit without a cached
set gets ONE base portrait, drawn through the same journalled path his
explicit chat pictures use, so the user allowlist, the worker's
fal Seedream renderer and the Claude checker all apply. The other frames stay missing; the stage then keeps the base for
them (its breathing and sway are CSS). Cached sets are never touched.

Bounds, all fail-closed:

- only users on ``KLUKAI_AGY_ALLOWED_USERS`` (the owner's account);
- one portrait job at a time in this process, inside the background-render
  marker, so his own picture waits for it instead of failing;
- at most ``KLUKAI_PORTRAIT_DAILY_LIMIT`` (default 3; 0 switches it off) new
  portraits per local day across all users, counted from the journal;
- never for an outfit that already has a base, never for one still locked at
  her affection level;
- always covered (no nudity grant, an SFW brief, intimate variants capped
  below the intimacy gate) and never with the Commander.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import uuid
from datetime import date

from PIL import Image

from . import account_images
from .image_gen_constants import INTIMATE_MIN_LEVEL
from .portrait import portrait_prompt
from .portrait_assets import (
    _to_webp,
    _write_atomic,
    existing_frames,
    frame_dir,
    frame_file,
)
from .portrait_style import HEIGHT, PALETTE_NEGATIVE, SOURCE_NAME, STYLE_VERSION, WIDTH

logger = logging.getLogger(__name__)

KIND = "portrait"
DAILY_LIMIT_ENV = "KLUKAI_PORTRAIT_DAILY_LIMIT"
DEFAULT_DAILY_LIMIT = 3
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "klukai:account-portrait:v1")

# The v3 look (portrait_style.COMPOSITION) in words: the stage's dark backdrop,
# soft side light, her face and shoulders centred like every cached set.
FRAMING = (
    "Live portrait for her chat window: Klukai alone, an upper-body portrait "
    "from the chest up with her head and shoulders well inside the frame, "
    "facing the viewer, centred and straight-on, looking at the viewer with a "
    "calm, neutral expression and a closed mouth. Backdrop: a dark navy-to-black "
    "gradient with soft bokeh and a few faint light particles, no scenery. Soft "
    "side lighting, gentle shading and only a light rim light. Nothing in front "
    "of her face, no other people, no text, frame or border. Keep her fully "
    "covered: front closed, no cleavage, no nudity."
)

_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


def _lock() -> asyncio.Lock:
    """One portrait job at a time (per event loop, so tests stay isolated)."""
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        _locks.clear()
        lock = _locks[loop] = asyncio.Lock()
    return lock


def daily_limit() -> int:
    """New account portraits allowed per local day; garbage or negative is 0."""
    raw = os.environ.get(DAILY_LIMIT_ENV, "").strip()
    if not raw:
        return DEFAULT_DAILY_LIMIT
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Ignoring non-numeric %s; account portraits are off", DAILY_LIMIT_ENV
        )
        return 0


def enabled_for(user_id: str) -> bool:
    """The account worker draws portraits for this (allowlisted) user."""
    from .agy_image_provider import uses_agy_images
    from .image_provider import selected_provider

    return (
        selected_provider() == "agy" and uses_agy_images(user_id) and daily_limit() > 0
    )


def request_id(user_id: str, outfit_id: str, day: date) -> str:
    """One journal id per (style, user, outfit, local day): a retry the same
    day recovers that request instead of drawing again."""
    return str(
        uuid.uuid5(
            _NAMESPACE, f"{STYLE_VERSION}:{user_id}:{outfit_id}:{day.isoformat()}"
        )
    )


def _visual(description: str) -> str:
    """The outfit's look, without the canon notes that follow it."""
    return " ".join(description.split("Canon anchor:", 1)[0].split()).strip()


def portrait_brief(outfit_id: str, level: int) -> str:
    """The worker brief: framing, the outfit in words, then her canon look."""
    from . import wardrobe
    from .image_provider import cloud_prompt

    covered = min(level, INTIMATE_MIN_LEVEL - 1)
    parts = [FRAMING]
    outfit = wardrobe.catalog().get(outfit_id)
    if outfit is not None:
        parts.append(f"She is wearing {outfit.name}: {_visual(outfit.description)}")
    parts.append(
        cloud_prompt(
            portrait_prompt(outfit_id, covered, "base"),
            sfw=True,
            negative_extra=PALETTE_NEGATIVE,
        )
    )
    return "\n\n".join(parts)


def fit_portrait(png: bytes) -> bytes:
    """Cover-crop (centred) and resize a render to the set's WIDTH x HEIGHT."""
    with Image.open(io.BytesIO(png)) as raw:
        image = raw.convert("RGB")
    target = WIDTH / HEIGHT
    w, h = image.size
    if w / h > target:
        crop_w = round(h * target)
        left = (w - crop_w) // 2
        image = image.crop((left, 0, left + crop_w, h))
    elif w / h < target:
        crop_h = round(w / target)
        top = (h - crop_h) // 2
        image = image.crop((0, top, w, top + crop_h))
    image = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


async def allowance_left(user_id: str, outfit_id: str) -> bool:
    """Today's id is already journalled (resumable), or a new one fits the cap."""
    try:
        today = account_images.local_day_start()
        if await account_images.fetch_request(
            request_id(user_id, outfit_id, today.date())
        ):
            return True
        return await account_images.count_requests_since(KIND, today) < daily_limit()
    except Exception as error:
        logger.warning("Portrait allowance unavailable (%s)", type(error).__name__)
        return False


async def draw_base(user_id: str, outfit_id: str, level: int) -> bool:
    """Draw and store the missing base frame. True once a base is on disk."""
    from . import wardrobe

    if "base" in existing_frames(user_id, outfit_id):
        return True
    if not enabled_for(user_id) or not wardrobe.is_unlocked(outfit_id, level):
        return False
    async with _lock():
        if "base" in existing_frames(user_id, outfit_id):
            return True
        today = account_images.local_day_start()
        rid = request_id(user_id, outfit_id, today.date())
        if await account_images.fetch_request(rid) is None:
            started = await account_images.count_requests_since(KIND, today)
            if started >= daily_limit():
                logger.info(
                    "Portrait daily limit reached (%d); %s waits", started, outfit_id
                )
                return False
        async with account_images.background_render(KIND):
            outcome = await account_images.fulfill_account_image(
                user_id,
                portrait_brief(outfit_id, level),
                width=WIDTH,
                height=HEIGHT,
                include_commander=False,
                request_id=rid,
                archive_context={
                    "kind": KIND,
                    "outfit": outfit_id,
                    "style": STYLE_VERSION,
                    "conversation_id": f"portrait:{outfit_id}",
                    "mood": "composed",
                    "affection_level": level,
                },
                nudity=False,
            )
        image = outcome.get("image")
        if not image:
            logger.info("Portrait %s not drawn (%s)", outfit_id, outcome.get("phase"))
            return False
        png = await asyncio.to_thread(fit_portrait, image)
        _write_atomic(frame_dir(user_id, outfit_id) / SOURCE_NAME, png)
        _write_atomic(
            frame_file(user_id, outfit_id, "base"),
            await asyncio.to_thread(_to_webp, png),
        )
        logger.info(
            "Portrait base drawn by the account worker: %s/%s", user_id, outfit_id
        )
        return True
