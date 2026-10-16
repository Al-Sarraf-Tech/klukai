"""Pinned portrait appearance, timing policy and mask-region parameters."""

from __future__ import annotations

import re

FRAME_NAMES = ("base", "blink", "talk", "smile", "blush", "annoyed")
SOURCE_NAME = "base.png"
WIDTH, HEIGHT = 832, 1216

# Canon palette anchors: pale silver hair (near-white, cool tint), natural pale
# skin. v2's heavier weights ((silver hair:1.4) + white + "pale blue-tinted
# white" + "soft colors") flattened the face into a white plane; v3 keeps the
# hair silver with lighter weights and anchors the skin explicitly, because
# bare shoulders otherwise drift tan/orange/pink under the lighting below.
PALETTE_ANCHORS = (
    "(silver hair:1.15), (white hair:0.8), (pale skin:1.15), natural skin tone"
)

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
    "detailed eyes, (eye reflection:1.1), shiny hair, " + PALETTE_ANCHORS
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
    "blink": (
        "(closed eyes:1.6), (eyelashes:1.1), neutral expression, closed mouth",
        0.85,
    ),
    "talk": ("looking at viewer, (open mouth:1.3), talking", 0.70),
    "smile": ("looking at viewer, (smile:1.3), happy", 0.75),
    "blush": ("(blush:1.3), embarrassed, looking away, wavy mouth", 0.75),
    "annoyed": (
        "looking at viewer, (frown:1.3), annoyed, v-shaped eyebrows, closed mouth",
        0.70,
    ),
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

START_DELAY_S = 45  # let a fresh connect settle (and the chat model warm)
QUIET_GAP_S = 60  # render only after a minute without a message from him
QUIET_POLL_S = 5
QUIET_MAX_WAIT_S = 30 * 60  # then give up; the next GET re-enqueues
FAILURE_BACKOFF_S = 300
SIGN_BUCKET_S = 900  # frame URLs are stable for a bucket, valid 15-30 min

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
CROP_SIDE_D = 3.4  # crop side, in eye distances
CROP_CENTER_DY = 0.3  # crop centre sits this far below the eye line
# Her identity tags ("green eyes, beautiful detailed eyes, expressive eyes")
# pull the blink back open; this branch-only negative counters them.
FRAME_NEGATIVES = {"blink": "(open eyes:1.3), green eyes, pupils, looking at viewer"}
BLINK_RETRY_DENOISE = (
    0.06  # a rejected blink is re-rolled at +0.06 denoise (0.85 → 0.91 → 0.97)
)
BLINK_OPEN_RATIO = 0.25  # a blink keeping >25% of the base's iris pixels didn't close
# When every re-roll still fails (live: blazing_star, whose cap shades the
# eyes), keep the most-closed attempt if it hides at least half the iris — a
# blink is on screen for ~110 ms and passes through half-closed anyway.
BLINK_FALLBACK_RATIO = 0.5
# Only a rejected branch is re-run, and only blink can be rejected, so extra
# re-rolls cost ~2 s each on a 576 px crop: 0.85 → 0.91 → 0.97 → 1.0 …
BLINK_RETRIES = 5
BLINK_MIN_IRIS = 40  # fewer iris pixels than this in the base: can't judge, accept

# Idle backfill of the other unlocked outfits (opt-in: PORTRAIT_BACKFILL=1).
BACKFILL_SILENCE_S = 30 * 60

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
