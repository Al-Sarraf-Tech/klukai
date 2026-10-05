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
from colorsys import rgb_to_hsv
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from . import context, signed_urls
from .image_gen import build_prompt

logger = logging.getLogger(__name__)

FRAME_NAMES = ("base", "blink", "talk", "smile", "blush", "annoyed")
SOURCE_NAME = "base.png"
WIDTH, HEIGHT = 832, 1216

# Canon palette anchors: pale silver hair (near-white, cool tint), natural pale
# skin. v2's heavier weights ((silver hair:1.4) + white + "pale blue-tinted
# white" + "soft colors") flattened the face into a white plane; v3 keeps the
# hair silver with lighter weights and anchors the skin explicitly, because
# bare shoulders otherwise drift tan/orange/pink under the lighting below.
PALETTE_ANCHORS = "(silver hair:1.15), (white hair:0.8), (pale skin:1.15), natural skin tone"

# The portrait negative (palette guard, form, backdrop, closures). Tuned on
# live renders of all 19 outfits as the test user:
# - hair/skin: no cyan/blue hair; no blue, pink, red, tan or orange skin
#   (strapless outfits went pink-red, hoodies tan at the neck);
# - form: the v2 look was posterized and unshaded, so flat color, no
#   shading, high contrast, posterization and overexposure are pushed away,
#   and glowing eyes (her irises rendered as neon discs);
# - backdrop: no white/light/plain or glowing background (banning only a
#   pure black one turned the backdrop near-white);
# - closures: portraits never show an unzipped front or cleavage (the
#   Klukadile onesie and the pajama shirt opened below level 8 without it);
# - Blazing Star's black leotard read royal blue under the rim light.
PALETTE_NEGATIVE = (
    "(blue hair:1.2), (cyan hair:1.3), aqua hair, oversaturated, "
    "(blue skin:1.2), (pink skin:1.3), (red skin:1.2), (tan:1.2), (dark skin:1.2), (orange skin:1.2), "
    "flat color, (no shading:1.2), monochrome, (glowing eyes:1.3), "
    "(high contrast:1.2), (posterization:1.2), (overexposed:1.2), "
    "(white background:1.2), (light background:1.2), (simple background:1.1), "
    "(glowing background:1.2), (bright background:1.2), lens flare, "
    "(unzipped:1.4), (cleavage:1.2), (blue leotard:1.2), (blue bodysuit:1.2)"
)
# Shared quality tags dropped for portraits only: "vivid colors" fights the
# canon palette, and film grain speckles the dark backdrop.
_PORTRAIT_DROPPED_TAGS = ("vivid colors, ", ", film grain")

# v3: form and depth instead of a flat cut-out on black. A dark gradient with
# a hint of navy, soft bokeh and a few light particles blends into the
# companion stage (#12151e) while giving depth; soft lighting from the side,
# strong soft shading and only a light rim give her face and hair volume; eye
# reflections give the eyes life. Strong blue backdrop weights turned the
# whole frame royal blue; none (or "blue background" in the negative) went
# pure black with no depth.
COMPOSITION = (
    "solo, portrait, upper body, facing viewer, centered, straight-on, "
    "(dark gradient background:1.2), (dark blue background:0.7), (bokeh:1.0), "
    "depth of field, blurry background, (light particles:0.7), "
    "(soft lighting:1.2), (soft shading:1.3), (sidelighting:0.9), (rim lighting:0.5), "
    "detailed eyes, (eye reflection:1.1), shiny hair, "
    + PALETTE_ANCHORS
)
# Bump when COMPOSITION/style changes: frames live under a versioned root, so
# a new style is drawn fresh instead of serving stale cached frames.
STYLE_VERSION = "v3"
BASE_EXPRESSION = "looking at viewer, neutral expression, closed mouth"

# frame -> (expression tags, inpaint denoise). Tuned on live renders as the
# test user, judged on face crops at full resolution:
#   blink   0.30-0.60 unmasked: eyes never close and the body shading drifts;
#           masked 0.60 still open, 0.72 green iris remnants, 0.78 closes them
#           on one base but not on another with taller eyes; 0.85 closes them
#           cleanly on every base tried (the nose is outside blink's mask, so
#           the higher denoise cannot reach it).
#   talk    0.65 / 0.70 / 0.80 all clean (0.80 slightly larger mouth).
#   smile   0.60 glitches the eye when the eyes are in the mask; mouth-only
#           0.75 is clean.
#   blush   0.80 paints the nose pink; cheeks+mouth at 0.75 is clean.
#   annoyed 0.80 adds a pink nose; 0.70 gives the frown and brows cleanly.
# The mask regions (FRAME_REGIONS) matter as much as denoise: the nose is in
# no mask, and blink's per-eye ellipses stop above the tear mark.
EXPRESSIONS: dict[str, tuple[str, float]] = {
    "blink": ("(closed eyes:1.4), neutral expression, closed mouth", 0.85),
    "talk": ("looking at viewer, (open mouth:1.3), talking", 0.70),
    "smile": ("looking at viewer, (smile:1.3), happy", 0.75),
    "blush": ("(blush:1.3), embarrassed, looking away, wavy mouth", 0.75),
    "annoyed": ("looking at viewer, (frown:1.3), annoyed, v-shaped eyebrows, closed mouth", 0.70),
}

# Ellipses (cx, cy, rx, ry) in units of her eye distance d, relative to the
# midpoint between her eyes.
_EYES = [(-0.5, 0.0, 0.40, 0.28), (0.5, 0.0, 0.40, 0.28)]
_MOUTH = (0.0, 0.68, 0.34, 0.22)
FRAME_REGIONS: dict[str, list[tuple[float, float, float, float]]] = {
    "blink": _EYES,
    "talk": [_MOUTH],
    "smile": [(0.0, 0.66, 0.5, 0.28)],
    "blush": [(-0.55, 0.38, 0.32, 0.22), (0.55, 0.38, 0.32, 0.22), _MOUTH],
    "annoyed": [(0.0, -0.05, 0.9, 0.36), _MOUTH],
}
MASK_FEATHER = 0.06  # Gaussian radius, in eye distances

START_DELAY_S = 45          # let a fresh connect settle (and the chat model warm)
QUIET_GAP_S = 60            # render only after a minute without a message from him
QUIET_POLL_S = 5
QUIET_MAX_WAIT_S = 30 * 60  # then give up; the next GET re-enqueues
FAILURE_BACKOFF_S = 300
SIGN_BUCKET_S = 900         # frame URLs are stable for a bucket, valid 15-30 min

# ── Pipeline speed ───────────────────────────────────────────────────────────
# The whole set is one GPU lease and one model load: the base, then every
# missing expression as ONE ComfyUI graph that inpaints an upscaled crop of
# her head, composited back here at full resolution through the feathered
# mask (so nothing outside the mask changes). Measured live before the
# sampler change: base visible ~7.5 s, full set 18-19 s (was ~80 s).
#   sampler  the model's own (WORKFLOW_TEMPLATE: euler / normal / CFG 4.5)
#   steps    base 20 (template default is 24; cut for a faster first frame),
#            inpaint 12 on the crop. Not yet re-checked live with euler/CFG 4.5.
#   crop     3.4 eye-distances square around the face, sampled at 576 px;
#            a 2.6x crop at 640 px lacked context (red smears, odd pupils)
#   blink    verified (her green irises must be gone) and re-rolled with a
#            new seed and +0.06 denoise, up to twice
BASE_STEPS = 20
INPAINT_STEPS = 12
CROP_PX = 576
CROP_SIDE_D = 3.4     # crop side, in eye distances
CROP_CENTER_DY = 0.3  # crop centre sits this far below the eye line
# Her identity tags ("green eyes, beautiful detailed eyes, expressive eyes")
# pull the blink back open; this branch-only negative counters them.
FRAME_NEGATIVES = {"blink": "(open eyes:1.3), green eyes, pupils, looking at viewer"}
BLINK_RETRY_DENOISE = 0.06  # a rejected blink is re-rolled at +0.06 denoise (0.85 → 0.91 → 0.97)
BLINK_OPEN_RATIO = 0.25  # a blink keeping >25% of the base's iris pixels didn't close
# When every re-roll still fails (live: blazing_star, whose cap shades the
# eyes), keep the most-closed attempt if it hides at least half the iris — a
# blink is on screen for ~110 ms and passes through half-closed anyway.
BLINK_FALLBACK_RATIO = 0.5
BLINK_MIN_IRIS = 40      # fewer iris pixels than this in the base: can't judge, accept

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


def portrait_prompt(outfit_id: str, level: int, frame: str) -> str:
    """The base or expression-frame prompt; only the expression tags differ."""
    expression = BASE_EXPRESSION if frame == "base" else EXPRESSIONS[frame][0]
    prompt = build_prompt(
        f"{expression}, {COMPOSITION}",
        affection_level=level, costume=outfit_id, mood="", request="",
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


@dataclass(frozen=True)
class Face:
    mid_x: float   # midpoint between her eyes, source pixels
    eye_y: float
    d: float       # eye distance
    found: bool = True


_SCAN = 4  # analyse at 1/4 resolution; plenty for two irises


def _green_blobs(img: Image.Image) -> list[tuple[float, float, int]]:
    """Centroids (source px) + sizes of saturated green blobs in the face zone."""
    small = img.convert("RGB").resize((max(1, img.width // _SCAN), max(1, img.height // _SCAN)))
    w, h = small.size
    data = small.tobytes()
    hits: set[tuple[int, int]] = set()
    for y in range(int(h * 0.08), int(h * 0.6)):
        for x in range(int(w * 0.15), int(w * 0.85)):
            i = (y * w + x) * 3
            hue, sat, val = rgb_to_hsv(data[i] / 255, data[i + 1] / 255, data[i + 2] / 255)
            if 0.13 <= hue <= 0.45 and sat >= 0.45 and val >= 0.35:
                hits.add((x, y))
    blobs = []
    while hits:
        stack = [hits.pop()]
        pts = []
        while stack:
            cx, cy = stack.pop()
            pts.append((cx, cy))
            for n in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                if n in hits:
                    hits.remove(n)
                    stack.append(n)
        if len(pts) >= 4:
            blobs.append((sum(p[0] for p in pts) / len(pts) * _SCAN,
                          sum(p[1] for p in pts) / len(pts) * _SCAN, len(pts)))
    return blobs


def locate_face(png: bytes) -> Face:
    """Find her eyes (Klukai's canon green irises) in a portrait base.

    Picks the best-matched pair of green blobs: level, similar size, a
    plausible eye distance, close to centre. Falls back to where the fixed
    portrait composition puts the face.
    """
    with Image.open(io.BytesIO(png)) as img:
        W, H = img.size
        blobs = _green_blobs(img)
    best: tuple[float, Face] | None = None
    for i, a in enumerate(blobs):
        for b in blobs[i + 1:]:
            (x1, y1, n1), (x2, y2, n2) = sorted((a, b))
            d = x2 - x1
            if not 0.06 * W <= d <= 0.3 * W or abs(y1 - y2) > 0.25 * d or max(n1, n2) > 3 * min(n1, n2):
                continue
            score = n1 + n2 - abs((x1 + x2) / 2 - W / 2) / W * 200
            if best is None or score > best[0]:
                best = (score, Face((x1 + x2) / 2, (y1 + y2) / 2, d))
    if best:
        return best[1]
    logger.warning("Portrait eyes not found; using the composition's default face box")
    return Face(W * 0.5, H * 0.27, W * 0.16, found=False)


def frame_mask(size: tuple[int, int], face: Face, frame: str) -> bytes:
    """A feathered white-on-black PNG mask of ``frame``'s repaint region."""
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for cx, cy, rx, ry in FRAME_REGIONS[frame]:
        x, y = face.mid_x + cx * face.d, face.eye_y + cy * face.d
        draw.ellipse((x - rx * face.d, y - ry * face.d, x + rx * face.d, y + ry * face.d), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(MASK_FEATHER * face.d))
    buf = io.BytesIO()
    mask.save(buf, "PNG")
    return buf.getvalue()


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


def face_box(face: Face, size: tuple[int, int]) -> tuple[int, int, int, int]:
    """The square head crop (clamped inside the image) the expressions inpaint."""
    w, h = size
    side = int(min(CROP_SIDE_D * face.d, w, h))
    x0 = int(max(0, min(w - side, face.mid_x - side / 2)))
    y0 = int(max(0, min(h - side, face.eye_y + CROP_CENTER_DY * face.d - side / 2)))
    return x0, y0, x0 + side, y0 + side


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _iris_pixels(img: Image.Image, mask: Image.Image) -> int:
    """Green-iris pixels under the white of ``mask`` (half resolution)."""
    box = mask.getbbox()
    if box is None:
        return 0
    small = (max(1, (box[2] - box[0]) // 2), max(1, (box[3] - box[1]) // 2))
    rgb = img.convert("RGB").crop(box).resize(small).tobytes()
    m = mask.crop(box).resize(small).tobytes()
    count = 0
    for i, weight in enumerate(m):
        if weight >= 128:
            hue, sat, val = rgb_to_hsv(rgb[3 * i] / 255, rgb[3 * i + 1] / 255, rgb[3 * i + 2] / 255)
            if 0.13 <= hue <= 0.45 and sat >= 0.45 and val >= 0.35:
                count += 1
    return count


class _SetPlan:
    """One set's geometry: where the face is, the crop, and each frame's mask."""

    def __init__(self, user_id: str, outfit_id: str, level: int, frames: list[str]):
        self.user_id, self.outfit_id, self.level, self.frames = user_id, outfit_id, level, frames
        self.base: Image.Image | None = None
        self.box = (0, 0, 1, 1)
        self.masks: dict[str, Image.Image] = {}

    async def __call__(self, base_png: bytes) -> tuple[bytes, list]:
        return await asyncio.to_thread(self._plan, base_png)

    def _plan(self, base_png: bytes) -> tuple[bytes, list]:
        from .image_gen import InpaintBranch

        self.base = Image.open(io.BytesIO(base_png)).convert("RGB")
        face = locate_face(base_png)
        self.box = face_box(face, self.base.size)
        branches = []
        for frame in self.frames:
            mask = Image.open(io.BytesIO(frame_mask(self.base.size, face, frame))).convert("L")
            self.masks[frame] = mask
            crop_mask = mask.crop(self.box).resize((CROP_PX, CROP_PX), Image.Resampling.LANCZOS)
            branches.append(InpaintBranch(
                frame, portrait_prompt(self.outfit_id, self.level, frame),
                EXPRESSIONS[frame][1], _png(crop_mask), FRAME_NEGATIVES.get(frame, ""),
            ))
        source = self.base.crop(self.box).resize((CROP_PX, CROP_PX), Image.Resampling.LANCZOS)
        return _png(source), branches

    def composite(self, frame: str, crop_png: bytes) -> Image.Image:
        """Lay an inpainted crop back on the base: only the mask's white changes."""
        assert self.base is not None
        x0, y0, x1, y1 = self.box
        with Image.open(io.BytesIO(crop_png)) as raw:
            painted = raw.convert("RGB").resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
        region = Image.composite(painted, self.base.crop(self.box), self.masks[frame].crop(self.box))
        full = self.base.copy()
        full.paste(region, (x0, y0))
        return full

    def _open_ratio(self, frame: str, crop_png: bytes) -> float:
        """Share of the base's iris pixels still visible in a render (0 = shut)."""
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
        ratio, best = min(((self._open_ratio(frame, p), i) for i, p in enumerate(crop_pngs)))
        return crop_pngs[best] if ratio <= BLINK_FALLBACK_RATIO else None


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
            fallback=plan.fallback,
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
