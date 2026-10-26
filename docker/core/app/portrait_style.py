"""Pinned portrait appearance and timing policy."""

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

# frame -> (expression tags, inpaint denoise). portrait_prompt() reads the
# tags; the denoise belonged to the retired local inpaint renderer (pictures
# are cloud-only since 2026-10-26). Tuned on live renders as the test user,
# judged on face crops at full resolution:
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

START_DELAY_S = 45  # let a fresh connect settle (and the chat model warm)
QUIET_GAP_S = 60  # render only after a minute without a message from him
QUIET_POLL_S = 5
QUIET_MAX_WAIT_S = 30 * 60  # then give up; the next GET re-enqueues
FAILURE_BACKOFF_S = 300
SIGN_BUCKET_S = 900  # frame URLs are stable for a bucket, valid 15-30 min

# Idle backfill of the other unlocked outfits (opt-in: PORTRAIT_BACKFILL=1).
BACKFILL_SILENCE_S = 30 * 60

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
