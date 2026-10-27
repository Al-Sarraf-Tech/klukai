"""Cached Live Portrait frames, and the account worker's base-frame chain.

Portrait polling, wardrobe changes and idle backfill never start generation.
Existing signed frames remain available and are never discarded: there is no
local renderer to redraw a set (pictures are cloud-only), so a cached set is
kept as drawn. :func:`ensure_generation` draws a missing base through the
account worker (:mod:`app.portrait_account`).
"""

from __future__ import annotations
from typing import Any


import asyncio
import logging
import io
from PIL import Image
from .portrait_geometry import (
    locate_face,
    face_box,
    frame_mask,
    _png as _png,
    _iris_pixels,
)
from .portrait_style import CROP_PX, BLINK_OPEN_RATIO, BLINK_MIN_IRIS
import time

from .runtime_tasks import ApplicationClosing, spawn
from .image_gen import build_prompt

from .portrait_assets import (
    portraits_root as portraits_root,
    frame_dir as frame_dir,
    frame_file as frame_file,
    existing_frames as existing_frames,
    _resource as _resource,
    _sign_ttl as _sign_ttl,
    frame_url as frame_url,
    verify_frame as verify_frame,
    _to_webp as _to_webp,
    _write_atomic as _write_atomic,
)

from .portrait_style import (
    FRAME_NAMES as FRAME_NAMES,
    SOURCE_NAME as SOURCE_NAME,
    WIDTH as WIDTH,
    HEIGHT as HEIGHT,
    PALETTE_ANCHORS as PALETTE_ANCHORS,
    PALETTE_NEGATIVE as PALETTE_NEGATIVE,
    _PORTRAIT_DROPPED_TAGS as _PORTRAIT_DROPPED_TAGS,
    COMPOSITION as COMPOSITION,
    STYLE_VERSION as STYLE_VERSION,
    BASE_EXPRESSION as BASE_EXPRESSION,
    EXPRESSIONS as EXPRESSIONS,
    START_DELAY_S as START_DELAY_S,
    QUIET_GAP_S as QUIET_GAP_S,
    QUIET_POLL_S as QUIET_POLL_S,
    QUIET_MAX_WAIT_S as QUIET_MAX_WAIT_S,
    FAILURE_BACKOFF_S as FAILURE_BACKOFF_S,
    SIGN_BUCKET_S as SIGN_BUCKET_S,
    BACKFILL_SILENCE_S as BACKFILL_SILENCE_S,
    _SAFE_ID as _SAFE_ID,
)

logger = logging.getLogger(__name__)


_inflight: dict[str, asyncio.Task[Any]] = {}
_failed_at: dict[str, float] = {}


# ── Layout ──────────────────────────────────────────────────────────────────


def _key(user_id: str, outfit_id: str) -> str:
    return f"{user_id}/{outfit_id}"


def portrait_prompt(outfit_id: str, level: int, frame: str) -> str:
    """The base or expression-frame prompt; only the expression tags differ."""
    expression = BASE_EXPRESSION if frame == "base" else EXPRESSIONS[frame][0]
    prompt = build_prompt(
        f"{expression}, {COMPOSITION}",
        affection_level=level,
        costume=outfit_id,
        mood="",
        request="",
        affection_tags=False,
    )
    for tag in _PORTRAIT_DROPPED_TAGS:
        prompt = prompt.replace(tag, "")
    return prompt


async def current_outfit(user_id: str, level: int) -> str:
    """What she is wearing right now (Her Day), Blazing Star on any failure."""
    from . import her_day, wardrobe

    try:
        return (await her_day.her_now(user_id, level)).outfit.id
    except Exception as e:
        logger.debug("Portrait outfit fell back to default: %s", e)
        return wardrobe.DEFAULT_OUTFIT


# ── Generation chain ─────────────────────────────────────────────────────────


def seconds_since_user_message() -> float | None:
    """Seconds since the Commander last sent a message (None: not recorded in
    this process). The cloud-only router no longer records it, so this is None
    (no wait) unless ``llm_router._last_user_message`` is provided again."""
    from . import llm_router

    last = getattr(llm_router, "_last_user_message", 0.0)
    return None if not last else time.monotonic() - last


async def _wait_for_quiet() -> bool:
    """Hold until he has been quiet for QUIET_GAP_S, so a render never sits
    between him and a reply. False if the chat never goes quiet."""
    waited = 0.0
    while True:
        gap = seconds_since_user_message()
        if gap is None or gap >= QUIET_GAP_S:
            return True
        if waited >= QUIET_MAX_WAIT_S:
            return False
        await asyncio.sleep(QUIET_POLL_S)
        waited += QUIET_POLL_S


def _fail(key: str) -> bool:
    _failed_at[key] = time.monotonic()
    return False


def _backing_off(key: str) -> bool:
    failed = _failed_at.get(key)
    if failed is None:
        return False
    if time.monotonic() - failed >= FAILURE_BACKOFF_S:
        _failed_at.pop(key, None)
        return False
    return True


async def _chain(user_id: str, outfit_id: str, level: int, delay_s: float = 0) -> bool:
    """Draw the missing base through the account worker, after a quiet gap in
    the chat. True once a base is on disk. The other frames stay missing."""
    key = _key(user_id, outfit_id)
    try:
        await asyncio.sleep(delay_s)
        if "base" in existing_frames(user_id, outfit_id):
            return True
        if not await _wait_for_quiet():
            return False
        from . import portrait_account

        if await portrait_account.draw_base(user_id, outfit_id, level):
            return True
        return _fail(key)
    except Exception as e:
        logger.warning("Portrait base %s failed (%s)", key, type(e).__name__)
        return _fail(key)


def ensure_generation(
    user_id: str, outfit_id: str, level: int, *, delay_s: float = 0
) -> bool:
    """Enqueue the chain unless one is running or backing off. True if enqueued.

    ``delay_s`` is for background pre-generation; when he is looking at the
    portrait there is no artificial wait (the chat-quiet gate still applies).
    """
    key = _key(user_id, outfit_id)
    if key in _inflight or _backing_off(key):
        return False
    from . import portrait_account

    if not portrait_account.enabled_for(user_id):
        return False
    try:
        task = spawn(_chain(user_id, outfit_id, level, delay_s))
    except ApplicationClosing:
        return False
    _inflight[key] = task

    def _done(_task: asyncio.Task[Any], k: str = key) -> None:
        _inflight.pop(k, None)

    task.add_done_callback(_done)
    return True


# ── Pre-generation: draw it before he asks ───────────────────────────────────

_background: set[asyncio.Task[Any]] = set()


async def prewarm(user_id: str, level: int) -> bool:
    """Outfit changes never authorize a picture."""
    return False


def prewarm_soon(user_id: str, level: int) -> None:
    """Retained for wardrobe callers; automatic portrait generation is disabled."""
    return


def backfill_enabled() -> bool:
    """Standing on-demand policy overrides legacy PORTRAIT_BACKFILL settings."""
    return False


async def backfill_tick(user_id: str) -> str | None:
    """Idle time never authorizes a picture."""
    return None


# ── API-facing state ─────────────────────────────────────────────────────────


async def portrait_state(user_id: str, outfit_id: str, level: int) -> dict[str, Any]:
    """Return cached signed frames without enqueuing any generation."""
    have = existing_frames(user_id, outfit_id)
    frames: dict[str, str | None] = {
        f: frame_url(
            user_id,
            outfit_id,
            f,
            int(frame_file(user_id, outfit_id, f).stat().st_mtime),
        )
        if f in have
        else None
        for f in FRAME_NAMES
    }
    if _key(user_id, outfit_id) in _inflight and len(have) < len(FRAME_NAMES):
        status = "partial" if "base" in have else "pending"
    else:
        status = "ready" if "base" in have else "unavailable"
    return {"outfit": outfit_id, "status": status, "frames": frames}


class _SetPlan:
    """CPU crop and masks for cloud edits; no local inference or rendering."""

    def __init__(
        self, user_id: str, outfit_id: str, level: int, frames: list[str]
    ) -> None:
        self.frames = frames
        self.base: Image.Image | None = None
        self.box = (0, 0, 1, 1)
        self.masks: dict[str, Image.Image] = {}

    async def __call__(self, base_png: bytes) -> tuple[bytes, list[object]]:
        return await asyncio.to_thread(self._plan, base_png)

    def _plan(self, base_png: bytes) -> tuple[bytes, list[object]]:
        self.base = Image.open(io.BytesIO(base_png)).convert("RGB")
        face = locate_face(base_png)
        self.box = face_box(face, self.base.size)
        for frame in self.frames:
            self.masks[frame] = Image.open(
                io.BytesIO(frame_mask(self.base.size, face, frame))
            ).convert("L")
        crop = self.base.crop(self.box).resize(
            (CROP_PX, CROP_PX), Image.Resampling.LANCZOS
        )
        return _png(crop), []

    def composite(self, frame: str, crop_png: bytes) -> Image.Image:
        assert self.base is not None
        x0, y0, x1, y1 = self.box
        with Image.open(io.BytesIO(crop_png)) as raw:
            painted = raw.convert("RGB").resize(
                (x1 - x0, y1 - y0), Image.Resampling.LANCZOS
            )
        region = Image.composite(
            painted, self.base.crop(self.box), self.masks[frame].crop(self.box)
        )
        full = self.base.copy()
        full.paste(region, (x0, y0))
        return full

    def accept(self, frame: str, crop_png: bytes) -> bool:
        if frame != "blink" or self.base is None:
            return True
        before = _iris_pixels(self.base, self.masks[frame])
        if before < BLINK_MIN_IRIS:
            return True
        after = _iris_pixels(self.composite(frame, crop_png), self.masks[frame])
        return after / before <= BLINK_OPEN_RATIO


def animate(user_id: str, outfit_id: str, level: int) -> bool:
    """One explicit button press may fill missing cloud blink/talk frames."""
    from . import portrait_account, portrait_cloud_frames

    key = _key(user_id, outfit_id)
    if (
        key in _inflight
        or _backing_off(key)
        or not portrait_account.enabled_for(user_id)
    ):
        return False

    async def run() -> None:
        try:
            if not await portrait_cloud_frames.draw_missing(user_id, outfit_id, level):
                _fail(key)
        except Exception as error:
            logger.warning("Portrait animation failed (%s)", type(error).__name__)
            _fail(key)
        finally:
            _inflight.pop(key, None)

    try:
        _inflight[key] = spawn(run())
    except ApplicationClosing:
        return False
    return True
