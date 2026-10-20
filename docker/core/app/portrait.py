"""Live Portrait: Klukai's face, in whatever she is wearing today.

Per (user, outfit) the app keeps six frames under
``/images/portraits/{STYLE_VERSION}/{user_id}/{outfit_id}/``:

- ``base.webp``: a txt2img portrait (fixed composition: upper body, facing
  the viewer, centred, a dark gradient backdrop with depth) with a deterministic seed per
  (user, outfit). Its lossless source is kept as ``base.png``.
- ``blink``, ``talk``, ``smile``, ``blush`` and ``annoyed``: masked img2img
  (inpaint) of that base. Her eyes are located in the base (her canon green
  irises), each frame repaints only its own feathered region (eyes, mouth,
  cheeks, brows) and is composited back onto the base, so every other pixel
  is identical and the frontend can crossfade between frames.

Generation is lazy and idempotent. A request for missing frames enqueues one
background chain per (user, outfit) that renders one image at a time through
the normal ``image_gen`` lease path. The chain waits for a quiet gap in the
chat (it never competes with a reply for the GPU), stands down while a game
owns the GPU, and backs off after a failure. Frame URLs are short-lived HMAC
signatures (``app/signed_urls.py``), so they work as a plain ``<img src>``.

That inpainted set needs ComfyUI on Dominus, so it is drawn only with
``KLUKAI_IMAGE_PROVIDER=comfyui`` (:func:`uses_gpu`). Otherwise Dominus is
never asked (no game probe, no lease): an outfit without a base gets one base
frame from the account worker (``app/portrait_account.py``), a base alone is
"ready" and the missing frames fall back to it, and a cached set is never
thrown away or redrawn.
"""

from __future__ import annotations
from typing import Any


import asyncio
import hashlib
import io
import logging
import os
import time

from PIL import Image

from . import context
from .runtime_tasks import ApplicationClosing, join, spawn
from .image_gen import build_prompt, InpaintBranch

from .portrait_geometry import (
    Face as Face,
    _green_blobs as _green_blobs,
    locate_face as locate_face,
    frame_mask as frame_mask,
    face_box as face_box,
    _png as _png,
    _iris_pixels as _iris_pixels,
)

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
    _EYES as _EYES,
    _MOUTH as _MOUTH,
    FRAME_REGIONS as FRAME_REGIONS,
    MASK_FEATHER as MASK_FEATHER,
    START_DELAY_S as START_DELAY_S,
    QUIET_GAP_S as QUIET_GAP_S,
    QUIET_POLL_S as QUIET_POLL_S,
    QUIET_MAX_WAIT_S as QUIET_MAX_WAIT_S,
    FAILURE_BACKOFF_S as FAILURE_BACKOFF_S,
    SIGN_BUCKET_S as SIGN_BUCKET_S,
    BASE_STEPS as BASE_STEPS,
    INPAINT_STEPS as INPAINT_STEPS,
    CROP_PX as CROP_PX,
    CROP_SIDE_D as CROP_SIDE_D,
    CROP_CENTER_DY as CROP_CENTER_DY,
    FRAME_NEGATIVES as FRAME_NEGATIVES,
    BLINK_RETRY_DENOISE as BLINK_RETRY_DENOISE,
    BLINK_OPEN_RATIO as BLINK_OPEN_RATIO,
    BLINK_FALLBACK_RATIO as BLINK_FALLBACK_RATIO,
    BLINK_RETRIES as BLINK_RETRIES,
    BLINK_MIN_IRIS as BLINK_MIN_IRIS,
    BACKFILL_SILENCE_S as BACKFILL_SILENCE_S,
    _SAFE_ID as _SAFE_ID,
)

logger = logging.getLogger(__name__)


_inflight: dict[str, asyncio.Task[Any]] = {}
_failed_at: dict[str, float] = {}


# ── Layout ──────────────────────────────────────────────────────────────────


def _key(user_id: str, outfit_id: str) -> str:
    return f"{user_id}/{outfit_id}"


def uses_gpu() -> bool:
    """The inpainted ComfyUI set (Dominus) only on the explicit legacy option."""
    from .image_provider import selected_provider

    return selected_provider() == "comfyui"


def can_redraw() -> bool:
    """Refresh throws a set away to draw it again: only where a full set can be
    drawn again. Off the GPU a cached set is kept as drawn."""
    return uses_gpu()


def _complete(user_id: str, outfit_id: str) -> bool:
    """Nothing more will be drawn: every frame (GPU), or the base (account)."""
    have = existing_frames(user_id, outfit_id)
    return have == set(FRAME_NAMES) if uses_gpu() else "base" in have


def portrait_seed(user_id: str, outfit_id: str) -> int:
    """Same Commander + same outfit → same face, framing and pose."""
    digest = hashlib.sha256(f"portrait:{user_id}:{outfit_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


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


# ── Face location + region masks ────────────────────────────────────────────


_SCAN = 4  # analyse at 1/4 resolution; plenty for two irises


# ── Signed frame URLs ────────────────────────────────────────────────────────


# ── Files ────────────────────────────────────────────────────────────────────


# ── Generation chain ─────────────────────────────────────────────────────────


def seconds_since_user_message() -> float | None:
    """Seconds since the Commander last sent a message (None: not this process)."""
    from . import llm_router

    last = llm_router._last_user_message
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


async def _clear_to_render() -> bool:
    return await _wait_for_quiet() and not await context.router.is_game_active()


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


class _SetPlan:
    """One set's geometry: where the face is, the crop, and each frame's mask."""

    def __init__(
        self, user_id: str, outfit_id: str, level: int, frames: list[str]
    ) -> None:
        self.user_id, self.outfit_id, self.level, self.frames = (
            user_id,
            outfit_id,
            level,
            frames,
        )
        self.base: Image.Image | None = None
        self.box = (0, 0, 1, 1)
        self.masks: dict[str, Image.Image] = {}

    async def __call__(self, base_png: bytes) -> tuple[bytes, list[InpaintBranch]]:
        return await asyncio.to_thread(self._plan, base_png)

    def _plan(self, base_png: bytes) -> tuple[bytes, list[InpaintBranch]]:
        from .image_gen import InpaintBranch

        self.base = Image.open(io.BytesIO(base_png)).convert("RGB")
        face = locate_face(base_png)
        self.box = face_box(face, self.base.size)
        branches = []
        for frame in self.frames:
            mask = Image.open(
                io.BytesIO(frame_mask(self.base.size, face, frame))
            ).convert("L")
            self.masks[frame] = mask
            crop_mask = mask.crop(self.box).resize(
                (CROP_PX, CROP_PX), Image.Resampling.LANCZOS
            )
            branches.append(
                InpaintBranch(
                    frame,
                    portrait_prompt(self.outfit_id, self.level, frame),
                    EXPRESSIONS[frame][1],
                    _png(crop_mask),
                    FRAME_NEGATIVES.get(frame, ""),
                )
            )
        source = self.base.crop(self.box).resize(
            (CROP_PX, CROP_PX), Image.Resampling.LANCZOS
        )
        return _png(source), branches

    def composite(self, frame: str, crop_png: bytes) -> Image.Image:
        """Lay an inpainted crop back on the base: only the mask's white changes."""
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

    def _open_ratio(self, frame: str, crop_png: bytes) -> float:
        """Share of the base's iris pixels still visible in a render (0 = shut)."""
        assert self.base is not None
        before = _iris_pixels(self.base, self.masks[frame])
        if before < BLINK_MIN_IRIS:
            return 0.0
        return _iris_pixels(self.composite(frame, crop_png), self.masks[frame]) / before

    def accept(self, frame: str, crop_png: bytes) -> bool:
        """Blink must actually close her eyes; every other frame is taken as is."""
        if frame != "blink" or self.base is None:
            return True
        return self._open_ratio(frame, crop_png) <= BLINK_OPEN_RATIO

    def fallback(self, frame: str, crop_pngs: list[bytes]) -> bytes | None:
        """After the last re-roll: the most-closed blink, if it's at least half shut."""
        if frame != "blink" or self.base is None or not crop_pngs:
            return None
        ratio, best = min(
            ((self._open_ratio(frame, p), i) for i, p in enumerate(crop_pngs))
        )
        return crop_pngs[best] if ratio <= BLINK_FALLBACK_RATIO else None


async def _account_chain(
    user_id: str, outfit_id: str, level: int, delay_s: float = 0
) -> bool:
    """Off the GPU: draw the missing base through the account worker, after a
    quiet gap in the chat. True once a base is on disk."""
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


async def _chain(user_id: str, outfit_id: str, level: int, delay_s: float = 0) -> bool:
    """Render whatever is missing as ONE leased set: the base (unless it is
    already on disk), then every missing expression in a single graph. Each
    image is written the moment it lands. True when the set is complete.
    Off the GPU only the base is drawn (:func:`_account_chain`)."""
    if not uses_gpu():
        return await _account_chain(user_id, outfit_id, level, delay_s)
    key = _key(user_id, outfit_id)
    try:
        await asyncio.sleep(delay_s)
        from . import image_gen

        source = frame_dir(user_id, outfit_id) / SOURCE_NAME
        have = existing_frames(user_id, outfit_id)
        base_png = source.read_bytes() if source.is_file() and "base" in have else None
        if base_png is None:
            have = set()  # frames made from a lost base no longer match it
        missing = [f for f in EXPRESSIONS if f not in have]
        if base_png is not None and not missing:
            return True
        if not await _clear_to_render():
            return False

        plan = _SetPlan(user_id, outfit_id, level, missing)
        writes: list[asyncio.Task[Any]] = []

        def _write_frame(name: str, png: bytes) -> None:
            full = plan.composite(name, png)
            _write_atomic(frame_file(user_id, outfit_id, name), _to_webp(_png(full)))

        async def on_image(name: str, png: bytes) -> None:
            if name == "base":  # first, and shown straight away
                _write_atomic(source, png)
                _write_atomic(
                    frame_file(user_id, outfit_id, "base"),
                    await asyncio.to_thread(_to_webp, png),
                )
            else:  # encoded in parallel, off the GPU lease's critical path
                writes.append(
                    asyncio.create_task(asyncio.to_thread(_write_frame, name, png))
                )

        try:
            await image_gen.generate_inpaint_set(
                base_prompt=portrait_prompt(outfit_id, level, "base"),
                base_png=base_png,
                plan=plan,
                seed=portrait_seed(user_id, outfit_id),
                width=WIDTH,
                height=HEIGHT,
                sfw=level < image_gen.INTIMATE_MIN_LEVEL,
                negative_extra=PALETTE_NEGATIVE,
                base_sampling=image_gen.Sampling(steps=BASE_STEPS),
                inpaint_sampling=image_gen.Sampling(steps=INPAINT_STEPS),
                on_image=on_image,
                accept=plan.accept,
                retry_denoise_step=BLINK_RETRY_DENOISE,
                retries=BLINK_RETRIES,
                fallback=plan.fallback,
            )
        finally:
            await join(writes)
        for write in writes:
            write.result()
        if existing_frames(user_id, outfit_id) != set(FRAME_NAMES):
            return _fail(key)
        logger.info("Portrait set complete: %s", key)
        return True
    except Exception as e:
        logger.warning("Portrait chain %s failed: %s", key, e)
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
    if not uses_gpu():
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
    """Draw the set for what she is wearing now, in the background. True if enqueued."""
    outfit = await current_outfit(user_id, level)
    if _complete(user_id, outfit):
        return False
    return ensure_generation(user_id, outfit, level, delay_s=START_DELAY_S)


def prewarm_soon(user_id: str, level: int) -> None:
    """Fire-and-forget prewarm (today's outfit picked or changed). Never raises."""

    async def _run() -> None:
        try:
            await prewarm(user_id, level)
        except Exception as e:
            logger.debug("Portrait prewarm skipped: %s", e)

    try:
        task = spawn(_run())
    except RuntimeError:
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


def backfill_enabled() -> bool:
    return os.environ.get("PORTRAIT_BACKFILL", "") == "1"


async def backfill_tick(user_id: str) -> str | None:
    """Idle backfill (opt-in): start ONE missing set for an unlocked outfit she
    acknowledges, only while nothing else is drawing, no game owns the GPU and
    he has been silent for BACKFILL_SILENCE_S. Returns the outfit enqueued."""
    if not backfill_enabled() or _inflight:
        return None
    gap = seconds_since_user_message()
    if gap is not None and gap < BACKFILL_SILENCE_S:
        return None
    if uses_gpu() and await context.router.is_game_active():
        return None
    from . import wardrobe

    level = (await context.affection.get_state(user_id)).level
    for outfit in wardrobe.catalog().values():
        if not (
            wardrobe.is_unlocked(outfit.id, level)
            and wardrobe.is_visible(outfit, level)
        ):
            continue
        if _complete(user_id, outfit.id):
            continue
        if ensure_generation(user_id, outfit.id, level):
            return outfit.id
    return None


# ── API-facing state ─────────────────────────────────────────────────────────


async def portrait_state(
    user_id: str, outfit_id: str, level: int, *, game_active: bool
) -> dict[str, Any]:
    """The GET /api/portrait payload; enqueues generation when frames are missing.

    Off the GPU ``game_active`` does not apply: a base is "ready" (the missing
    frames fall back to it), no base is "pending" while one can be drawn
    today, else "unavailable" (her official portrait)."""
    key = _key(user_id, outfit_id)
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
    if not uses_gpu():
        status = await _account_status(user_id, outfit_id, level, have)
    elif len(have) == len(FRAME_NAMES):
        status = "ready"
    elif game_active or _backing_off(key):
        # Nothing more is coming right now; whatever exists is what she has.
        status = "ready" if "base" in have else "unavailable"
    else:
        ensure_generation(user_id, outfit_id, level)
        status = "partial" if "base" in have else "pending"
    return {"outfit": outfit_id, "status": status, "frames": frames}


async def _account_status(
    user_id: str, outfit_id: str, level: int, have: set[str]
) -> str:
    from . import portrait_account

    if "base" in have:
        return "ready"
    if _key(user_id, outfit_id) in _inflight:
        return "pending"  # already queued or drawing: no allowance lookup per poll
    if (
        _backing_off(_key(user_id, outfit_id))
        or not portrait_account.enabled_for(user_id)
        or not await portrait_account.allowance_left(user_id, outfit_id)
    ):
        return "unavailable"
    ensure_generation(user_id, outfit_id, level)
    return "pending"


def refresh(user_id: str, outfit_id: str, level: int) -> bool:
    """Throw the set away and render it again. Refuses while a chain runs,
    and always off the GPU (a cached set is never thrown away there)."""
    key = _key(user_id, outfit_id)
    if key in _inflight or not can_redraw():
        return False
    folder = frame_dir(user_id, outfit_id)
    if folder.is_dir():
        for path in folder.iterdir():
            if path.is_file():
                path.unlink()
    _failed_at.pop(key, None)
    ensure_generation(user_id, outfit_id, level)
    return True
