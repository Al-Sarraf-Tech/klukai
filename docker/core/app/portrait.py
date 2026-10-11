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

The drawing itself — the style, the prompt, eye location, masks, the head crop
and the set plan — lives in app/portrait_render.py and is re-exported here.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import time
import uuid
from pathlib import Path

from PIL import Image

from . import context, signed_urls
from .portrait_render import (
    BASE_STEPS, BLINK_RETRIES, BLINK_RETRY_DENOISE, EXPRESSIONS, INPAINT_STEPS, PALETTE_NEGATIVE, STYLE_VERSION,
    _png, _SetPlan, portrait_prompt,
)
# The rest of the drawing layer, re-exported (the redundant ``as`` marks it
# public): every surface reads portraits through this module and tests patch
# it here.
from .portrait_render import (
    BASE_EXPRESSION as BASE_EXPRESSION, BLINK_FALLBACK_RATIO as BLINK_FALLBACK_RATIO,
    BLINK_MIN_IRIS as BLINK_MIN_IRIS, BLINK_OPEN_RATIO as BLINK_OPEN_RATIO, COMPOSITION as COMPOSITION,
    CROP_CENTER_DY as CROP_CENTER_DY, CROP_PX as CROP_PX, CROP_SIDE_D as CROP_SIDE_D, FRAME_NEGATIVES as FRAME_NEGATIVES,
    FRAME_REGIONS as FRAME_REGIONS, MASK_FEATHER as MASK_FEATHER, PALETTE_ANCHORS as PALETTE_ANCHORS, Face as Face,
    _EYES as _EYES, _MOUTH as _MOUTH, _PORTRAIT_DROPPED_TAGS as _PORTRAIT_DROPPED_TAGS, _SCAN as _SCAN,
    _green_blobs as _green_blobs, _iris_pixels as _iris_pixels, face_box as face_box, frame_mask as frame_mask,
    locate_face as locate_face,
)

logger = logging.getLogger(__name__)

FRAME_NAMES = ("base", "blink", "talk", "smile", "blush", "annoyed")
SOURCE_NAME = "base.png"
WIDTH, HEIGHT = 832, 1216

START_DELAY_S = 45          # let a fresh connect settle (and the chat model warm)
QUIET_GAP_S = 60            # render only after a minute without a message from him
QUIET_POLL_S = 5
QUIET_MAX_WAIT_S = 30 * 60  # then give up; the next GET re-enqueues
FAILURE_BACKOFF_S = 300
SIGN_BUCKET_S = 900         # frame URLs are stable for a bucket, valid 15-30 min

# Idle backfill of the other unlocked outfits (opt-in: PORTRAIT_BACKFILL=1).
BACKFILL_SILENCE_S = 30 * 60

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

_inflight: dict[str, asyncio.Task] = {}
_failed_at: dict[str, float] = {}


# ── Layout ──────────────────────────────────────────────────────────────────


def portraits_root() -> Path:
    return Path(os.environ.get("IMAGES_DIR", "/images")) / "portraits" / STYLE_VERSION


def _key(user_id: str, outfit_id: str) -> str:
    return f"{user_id}/{outfit_id}"


def frame_dir(user_id: str, outfit_id: str) -> Path:
    """The frame directory; rejects anything that is not a plain id."""
    for part in (user_id, outfit_id):
        if not _SAFE_ID.fullmatch(part or ""):
            raise ValueError("unsafe portrait id")
    return portraits_root() / user_id / outfit_id


def frame_file(user_id: str, outfit_id: str, frame: str) -> Path:
    if frame not in FRAME_NAMES:
        raise ValueError(f"unknown portrait frame {frame!r}")
    return frame_dir(user_id, outfit_id) / f"{frame}.webp"


def existing_frames(user_id: str, outfit_id: str) -> set[str]:
    return {f for f in FRAME_NAMES if frame_file(user_id, outfit_id, f).is_file()}


def portrait_seed(user_id: str, outfit_id: str) -> int:
    """Same Commander + same outfit → same face, framing and pose."""
    digest = hashlib.sha256(f"portrait:{user_id}:{outfit_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big")



async def current_outfit(user_id: str, level: int) -> str:
    """What she is wearing right now (Her Day), Blazing Star on any failure."""
    from . import her_day, wardrobe

    try:
        return (await her_day.her_now(user_id, level)).outfit.id
    except Exception as e:
        logger.debug("Portrait outfit fell back to default: %s", e)
        return wardrobe.DEFAULT_OUTFIT


# ── Signed frame URLs ────────────────────────────────────────────────────────


def _resource(user_id: str, outfit_id: str, frame: str) -> str:
    return f"portrait:{user_id}/{outfit_id}/{frame}"


def _sign_ttl(now: float) -> int:
    """TTL that lands expiry on a bucket edge, so URLs stay byte-identical
    (one browser cache entry) for a whole bucket."""
    bucket_end = (int(now) // SIGN_BUCKET_S + 2) * SIGN_BUCKET_S
    return bucket_end - int(now)


def frame_url(user_id: str, outfit_id: str, frame: str, version: int) -> str:
    token = signed_urls.sign(
        _resource(user_id, outfit_id, frame),
        ttl_seconds=_sign_ttl(time.time()), user_id=user_id,
    )
    return f"/api/portrait/frame/{user_id}/{outfit_id}/{frame}.webp?v={version}&sig={token}"


def verify_frame(user_id: str, outfit_id: str, frame: str, sig: str) -> bool:
    return signed_urls.verify(sig, _resource(user_id, outfit_id, frame), user_id=user_id)


# ── Files ────────────────────────────────────────────────────────────────────


def _to_webp(png: bytes) -> bytes:
    buf = io.BytesIO()
    with Image.open(io.BytesIO(png)) as img:
        img.convert("RGB").save(buf, "WEBP", quality=92, method=4)
    return buf.getvalue()


def _write_atomic(path: Path, data: bytes) -> None:
    """Readers see the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)



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



async def _chain(user_id: str, outfit_id: str, level: int, delay_s: float = 0) -> bool:
    """Render whatever is missing as ONE leased set: the base (unless it is
    already on disk), then every missing expression in a single graph. Each
    image is written the moment it lands. True when the set is complete."""
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
        writes: list[asyncio.Task] = []

        def _write_frame(name: str, png: bytes) -> None:
            full = plan.composite(name, png)
            _write_atomic(frame_file(user_id, outfit_id, name), _to_webp(_png(full)))

        async def on_image(name: str, png: bytes) -> None:
            if name == "base":  # first, and shown straight away
                _write_atomic(source, png)
                _write_atomic(frame_file(user_id, outfit_id, "base"), await asyncio.to_thread(_to_webp, png))
            else:  # encoded in parallel, off the GPU lease's critical path
                writes.append(asyncio.create_task(asyncio.to_thread(_write_frame, name, png)))

        await image_gen.generate_inpaint_set(
            base_prompt=portrait_prompt(outfit_id, level, "base"),
            base_png=base_png, plan=plan, seed=portrait_seed(user_id, outfit_id),
            width=WIDTH, height=HEIGHT, sfw=level < image_gen.INTIMATE_MIN_LEVEL,
            negative_extra=PALETTE_NEGATIVE,
            base_sampling=image_gen.Sampling(steps=BASE_STEPS),
            inpaint_sampling=image_gen.Sampling(steps=INPAINT_STEPS),
            on_image=on_image, accept=plan.accept, retry_denoise_step=BLINK_RETRY_DENOISE,
            retries=BLINK_RETRIES, fallback=plan.fallback,
        )
        await asyncio.gather(*writes)
        if existing_frames(user_id, outfit_id) != set(FRAME_NAMES):
            return _fail(key)
        logger.info("Portrait set complete: %s", key)
        return True
    except Exception as e:
        logger.warning("Portrait chain %s failed: %s", key, e)
        return _fail(key)


def ensure_generation(user_id: str, outfit_id: str, level: int, *, delay_s: float = 0) -> bool:
    """Enqueue the chain unless one is running or backing off. True if enqueued.

    ``delay_s`` is for background pre-generation; when he is looking at the
    portrait there is no artificial wait (the chat-quiet gate still applies).
    """
    key = _key(user_id, outfit_id)
    if key in _inflight or _backing_off(key):
        return False
    task = asyncio.create_task(_chain(user_id, outfit_id, level, delay_s))
    _inflight[key] = task

    def _done(_task: asyncio.Task, k: str = key) -> None:
        _inflight.pop(k, None)

    task.add_done_callback(_done)
    return True



# ── Pre-generation: draw it before he asks ───────────────────────────────────

_background: set[asyncio.Task] = set()


async def prewarm(user_id: str, level: int) -> bool:
    """Draw the set for what she is wearing now, in the background. True if enqueued."""
    outfit = await current_outfit(user_id, level)
    if existing_frames(user_id, outfit) == set(FRAME_NAMES):
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
        task = asyncio.get_running_loop().create_task(_run())
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
    if await context.router.is_game_active():
        return None
    from . import wardrobe

    level = (await context.affection.get_state(user_id)).level
    for outfit in wardrobe.catalog().values():
        if not (wardrobe.is_unlocked(outfit.id, level) and wardrobe.is_visible(outfit, level)):
            continue
        if existing_frames(user_id, outfit.id) == set(FRAME_NAMES):
            continue
        if ensure_generation(user_id, outfit.id, level):
            return outfit.id
    return None


# ── API-facing state ─────────────────────────────────────────────────────────


async def portrait_state(user_id: str, outfit_id: str, level: int, *, game_active: bool) -> dict:
    """The GET /api/portrait payload; enqueues generation when frames are missing."""
    key = _key(user_id, outfit_id)
    have = existing_frames(user_id, outfit_id)
    frames: dict[str, str | None] = {
        f: frame_url(user_id, outfit_id, f, int(frame_file(user_id, outfit_id, f).stat().st_mtime))
        if f in have else None
        for f in FRAME_NAMES
    }
    if len(have) == len(FRAME_NAMES):
        status = "ready"
    elif game_active or _backing_off(key):
        # Nothing more is coming right now; whatever exists is what she has.
        status = "ready" if "base" in have else "unavailable"
    else:
        ensure_generation(user_id, outfit_id, level)
        status = "partial" if "base" in have else "pending"
    return {"outfit": outfit_id, "status": status, "frames": frames}


def refresh(user_id: str, outfit_id: str, level: int) -> bool:
    """Throw the set away and render it again. Refuses while a chain runs."""
    key = _key(user_id, outfit_id)
    if key in _inflight:
        return False
    folder = frame_dir(user_id, outfit_id)
    if folder.is_dir():
        for path in folder.iterdir():
            if path.is_file():
                path.unlink()
    _failed_at.pop(key, None)
    ensure_generation(user_id, outfit_id, level)
    return True
