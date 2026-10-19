"""Per-render variety — candid composition, hairstyle and small accessories (pure).

So pictures of her are not the same posed portrait every time: each render
picks a camera/framing cue and a candid action, may restyle her hair, and may
add one or two small things from her canon gift tastes (metal-framed glasses,
silver jewelry, a colourful hair accessory, the Turbokat cat-ear earphones) or
take the jacket off.

Never varied: her identity (silver hair, green eyes, the cross hair ornament,
the tear mark — KLUKAI_IDENTITY is untouched) and each outfit's fixed
elements. An outfit's ``hair`` / ``accessories`` list in the catalog overrides
its category default; an empty list fixes it. Every choice is a pure function
of a seed, so a render is reproducible and testable.

Live Portrait and explicit poses never come here (see build_prompt).
"""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Sequence

from .wardrobe_catalog import Outfit


def derive_seed(*parts: object) -> str:
    """A stable seed from the render's inputs (same inputs, same picture plan)."""
    material = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def variation_rng(seed: int | str, salt: str) -> random.Random:
    """One independent stream per pool, so a change to one pool never
    reshuffles the others."""
    digest = hashlib.sha256(f"{seed}:{salt}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


# ── Composition ──────────────────────────────────────────────────────────────
# SFW by construction: no low or high angles (from below/above read as
# upskirt/cleavage shots), no "from behind", nothing seductive.

CAMERA_CUES: tuple[str, ...] = (
    "cowboy shot, dutch angle",
    "upper body, from side",
    "full body, wide shot",
    "cowboy shot, looking to the side",
    "upper body, looking back",
    "full body, from side",
    "upper body, candid, looking away",
)

SOLO_ACTIONS: tuple[str, ...] = (
    "walking, mid-stride",
    "leaning against wall, arms crossed",
    "hand on own hip",
    "turning around, looking over shoulder",
    "tucking hair behind ear",
    "wind, floating hair",
    "hands behind back",
    "hand on own chin",
)

COUPLE_ACTIONS: tuple[str, ...] = (
    "walking side by side",
    "standing close together",
    "back to back",
    "looking at each other",
    "side by side, candid",
)

# Interaction with the scene, keyed by app/wardrobe_scene.py scene keys.
# An empty tuple = no action cue (a bedtime picture stays still).
SCENE_ACTIONS: dict[str, tuple[str, ...]] = {
    "bedtime": (),
    "training": ("drinking from water bottle", "stretching, warming up"),
    "garage": ("kneeling beside motorcycle, holding wrench", "wiping hands on rag"),
    "ride": ("leaning on motorcycle", "sitting on motorcycle, looking back"),
    "rain": ("looking up at the rain", "wringing out hair"),
    "snow": ("catching snowflakes", "breath visible, hands in pockets"),
    "beach": ("walking along the shore", "wind, floating hair"),
    "stargazing": ("looking up at the stars", "pointing at the sky"),
    "fireworks": ("looking up at fireworks",),
    "lanterns": ("holding lantern, looking up",),
    "tea": ("pouring tea", "holding tray"),
    "cat_cafe": ("petting a cat", "cat on lap"),
    "reading": ("reading a book", "holding stylus, drawing tablet"),
    "powersports": ("looking at motorcycles", "pointing"),
    "ceremony": ("standing at attention",),
}

_FRAMING = re.compile(
    r"\b(?:upper body|full body|cowboy shot|close-up|close up|portrait|wide shot|"
    r"from above|from below|from side|from behind|dutch angle|pov|profile|"
    r"headshot|bust shot|selfie|face focus)\b"
)
_POSED = re.compile(
    r"\b(?:sitting|seated|lying|kneeling|crouching|squatting|leaning|walking|running|"
    r"riding|dancing|jumping|curled up|on back|on stomach|hug|hugging|embrace|kiss|"
    r"kissing|cuddling|holding hands|carrying|action pose|fighting stance|combat stance|"
    r"pose|posing|arms crossed|hand on hip|reaching|stretching|sleeping|lying down|"
    r"on bed|salute|saluting|bowing|piggyback)\b"
)


def composition(
    scene: str | None,
    *,
    couple: bool = False,
    framing_text: str = "",
    seed: int | str = 0,
) -> str:
    """A candid camera cue and action for this render; parts the caller's own
    tags already set (framing, a pose) are left alone."""
    lower = framing_text.lower()
    parts: list[str] = []
    if not _FRAMING.search(lower):
        parts.append(variation_rng(seed, "camera").choice(CAMERA_CUES))
    if not _POSED.search(lower):
        if couple:
            pool: Sequence[str] = COUPLE_ACTIONS
        else:
            pool = SCENE_ACTIONS.get(scene or "", SOLO_ACTIONS)
        if pool:
            parts.append(variation_rng(seed, "action").choice(pool))
    return ", ".join(parts)


# ── Hair ─────────────────────────────────────────────────────────────────────

HAIR_STYLES: tuple[str, ...] = (
    "high ponytail",
    "hair down",
    "half updo",
    "single braid",
    "braided ponytail",
    "low ponytail",
    "single hair bun",
)
_HAIR_TAGS = frozenset(HAIR_STYLES) | {
    "ponytail",
    "hair bun",
    "side braid",
    "twintails",
}

# formal / oath / sleep are absent: those outfits keep their own hairstyle.
CATEGORY_HAIR: dict[str, tuple[str, ...]] = {
    "duty": ("high ponytail", "braided ponytail", "low ponytail"),
    "rider": ("high ponytail", "braided ponytail", "low ponytail"),
    "training": ("high ponytail", "braided ponytail", "single hair bun"),
    "work": ("high ponytail", "braided ponytail", "single hair bun"),
    "weather": ("high ponytail", "low ponytail", "braided ponytail", "hair down"),
    "swim": ("ponytail", "high ponytail", "braided ponytail"),
    "off_duty": (
        "high ponytail",
        "hair down",
        "half updo",
        "single braid",
        "low ponytail",
        "single hair bun",
    ),
    "seasonal": ("high ponytail", "hair down", "half updo", "single braid"),
}
# Her hair down is "a side others don't see" (layer_note): not below 3, unless
# the outfit itself is worn that way.
_HAIR_MIN_LEVEL = {"hair down": 3}
_NO_UPDO_UNDER = re.compile(r"\b(?:baseball cap|visor cap|hood up|animal hood)\b")
_UPDOS = frozenset({"half updo", "single hair bun"})


def _tags(image_tags: str) -> list[str]:
    return [t.strip() for t in image_tags.split(",") if t.strip()]


def own_hair(o: Outfit) -> str | None:
    return next((t for t in _tags(o.image_tags) if t in _HAIR_TAGS), None)


def hair_options(o: Outfit, level: int) -> list[str]:
    """Hairstyles this outfit may be drawn with at ``level`` (always incl. its own)."""
    own = own_hair(o)
    if own is None:
        return []
    pool = o.hair if o.hair is not None else CATEGORY_HAIR.get(o.category, ())
    options = [h for h in pool if h == own or level >= _HAIR_MIN_LEVEL.get(h, 0)]
    if _NO_UPDO_UNDER.search(o.image_tags.lower()):
        options = [h for h in options if h not in _UPDOS]
    if own not in options:
        options.insert(0, own)
    return options


def pick_hair(o: Outfit, level: int, rng: random.Random) -> str | None:
    """Half the time her outfit's own style; otherwise another allowed one."""
    own = own_hair(o)
    others = [h for h in hair_options(o, level) if h != own]
    if own is None or not others or rng.random() < 0.5:
        return own
    return rng.choice(others)


# ── Accessories ──────────────────────────────────────────────────────────────
# From her canon gift tastes (dossier §4): Metal-Framed Glasses, Melancholy
# Earrings, Heavy Metal Necklace, Themed Bracelet, Colorful Hair Accessories,
# and Astral Luminous's Turbokat Resonance cat-ear earphones. Silver only —
# gold is canon on the maid outfit alone.
ACCESSORIES: dict[str, str] = {
    "glasses": "round eyewear, glasses",
    "earrings": "silver earrings",
    "necklace": "chain necklace",
    "bracelet": "silver bracelet",
    "scrunchie": "purple scrunchie",
    "cat_headphones": "cat ear headphones",
    "jacket_off": "jacket around waist",
}

# formal / oath / sleep are absent: nothing is added to those.
CATEGORY_ACCESSORIES: dict[str, tuple[str, ...]] = {
    "duty": ("earrings", "scrunchie", "jacket_off"),
    "rider": ("earrings", "scrunchie", "cat_headphones"),
    "training": ("scrunchie", "jacket_off"),
    "work": ("scrunchie",),
    "weather": ("earrings", "scrunchie"),
    "swim": ("earrings", "bracelet"),
    "off_duty": (
        "glasses",
        "earrings",
        "necklace",
        "bracelet",
        "scrunchie",
        "cat_headphones",
        "jacket_off",
    ),
    "seasonal": ("earrings", "necklace", "bracelet"),
}
# The glasses are for screens and he was not supposed to see them (drawing_night
# opens at 4): not in his pictures before that.
_ACCESSORY_MIN_LEVEL = {"glasses": 4}

# Never contradict the outfit: skip a piece it already has, or one something
# in the way would hide or clash with (a cap under the earphones, a scarf over
# the necklace).
_CONFLICTS: dict[str, re.Pattern[str]] = {
    key: re.compile(r"\b(?:" + "|".join(words) + r")\b")
    for key, words in {
        "glasses": ("eyewear", "glasses", "veil"),
        "earrings": ("earrings",),
        "necklace": ("necklace", "choker", "scarf"),
        "bracelet": ("bracelet", "elbow gloves"),
        "scrunchie": ("scrunchie",),
        "cat_headphones": (
            "headwear",
            "cap",
            "hat",
            "visor",
            "hood up",
            "animal hood",
            "headdress",
            "veil",
            "tiara",
            "headphones",
            "cat ears",
            "helmet",
        ),
        "jacket_off": ("jacket around waist", "see-through jacket"),
    }.items()
}
# The tags that mean "jacket worn"; taking it off drops them.
_JACKET_WORN = frozenset(
    {"off shoulder", "open jacket", "cropped jacket", "zipped racing jacket"}
)
_HAS_JACKET = re.compile(r"\bjacket\b")
_TIED_HAIR = re.compile(r"ponytail|braid|bun")


def accessory_options(o: Outfit, level: int, hair: str | None = None) -> list[str]:
    """Accessory keys that fit this outfit (and hairstyle) at ``level``."""
    pool = (
        o.accessories
        if o.accessories is not None
        else CATEGORY_ACCESSORIES.get(o.category, ())
    )
    tags = o.image_tags.lower()
    out: list[str] = []
    for key in pool:
        if key not in ACCESSORIES or level < _ACCESSORY_MIN_LEVEL.get(key, 0):
            continue
        if _CONFLICTS[key].search(tags):
            continue
        if key == "scrunchie" and not _TIED_HAIR.search(hair or own_hair(o) or ""):
            continue
        if key == "jacket_off" and not _HAS_JACKET.search(tags):
            continue
        out.append(key)
    return out


def pick_accessories(
    o: Outfit, level: int, rng: random.Random, hair: str | None = None
) -> list[str]:
    """None, one or two small pieces (mostly one)."""
    options = accessory_options(o, level, hair)
    count = min(rng.choice((0, 1, 1, 2)), len(options))
    return rng.sample(options, count) if count else []


def styled_tags(o: Outfit, level: int, seed: int | str) -> str:
    """The outfit's tag block with this render's hairstyle and accessories."""
    tags = _tags(o.image_tags)
    hair = pick_hair(o, level, variation_rng(seed, "hair"))
    if hair is not None:
        at = next(i for i, t in enumerate(tags) if t in _HAIR_TAGS)
        tags = [t for i, t in enumerate(tags) if i == at or t not in _HAIR_TAGS]
        tags[at] = hair
    picked = pick_accessories(o, level, variation_rng(seed, "accessories"), hair)
    if "jacket_off" in picked:
        tags = [t for t in tags if t not in _JACKET_WORN]
    tags.extend(ACCESSORIES[key] for key in picked)
    return ", ".join(tags)
