"""Dress for the scene — which outfit a picture's scene calls for (pure).

When his request (or, with no request, the scene tags of a picture she draws
herself) clearly sets an activity or a place, she wears the kit for it instead
of today's base outfit: range kit at the range, coveralls in the hangar, the
rain shell in the rain. Only outfits she owns at the current affection level
count (fail closed: otherwise today's outfit stands), never a ``deny`` outfit
(she denies the onesie exists) and never one she would not acknowledge yet.

Precedence lives in app/image_prompts.py:build_prompt — his explicit outfit
request > the bath/bed intimacy rules > this > today's outfit. This module only
answers "which scene is this, and what would she wear for it".
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from .wardrobe_catalog import Outfit, canonical_id, catalog


@dataclass(frozen=True)
class Scene:
    key: str
    keywords: tuple[str, ...]
    # Candidates; one eligible outfit is picked per render.
    outfits: tuple[str, ...]


def _scene(key: str, keywords: str, *outfits: str) -> Scene:
    """``keywords``: comma-separated whole words or phrases."""
    words = tuple(k.strip() for k in keywords.split(",") if k.strip())
    return Scene(key, words, outfits)


# Priority order: the FIRST scene whose keyword appears (whole words) wins —
# "a ride in the rain" is the rain shell, "Racing Calm" is the tea counter,
# not a race. Keep keywords unambiguous: "draw"/"sketch" ask for a picture and
# "morning"/"night" only set the light, so none of them dress her.
SCENES: tuple[Scene, ...] = (
    _scene(
        "bedtime",
        "bed, bedtime, sleep, sleeping, asleep, pajamas, pyjamas, sleepwear, nightwear, "
        "goodnight, good night, insomnia, can't sleep, cant sleep, late watch, tucked in",
        "sleepless_watch",
    ),
    _scene("halloween", "halloween, trick or treat, trick-or-treat", "black_cat_ops"),
    _scene(
        "christmas",
        "christmas, xmas, christmas eve, christmas tree",
        "red_scarf_sortie",
    ),
    _scene(
        "fireworks",
        "fireworks, firework, sparkler, sparklers, new year, new year's, new years, "
        "new year's eve, countdown",
        "fireworks_party",
    ),
    _scene(
        "lanterns",
        "lantern, lanterns, mid-autumn, mid autumn, mooncake, mooncakes, moon festival, "
        "lantern festival, moon viewing, moon-viewing",
        "lantern_walk",
    ),
    _scene(
        "swim",
        "swim, swimming, swimsuit, bikini, pool, poolside, surf, surfing, surfboard, "
        "snorkeling",
        "cerulean_breaker",
    ),
    _scene(
        "beach",
        "beach, beaches, seaside, seashore, shore, shoreline, boardwalk, coast, sandcastle",
        "cerulean_breaker",
        "terrace_cover_up",
    ),
    _scene(
        "training",
        "training, gym, workout, work out, working out, exercise, exercising, "
        "firing range, shooting range, the range, target practice, drills, sparring, pt, "
        "jogging, morning run, push-ups, pushups, push ups",
        "range_kit",
    ),
    _scene(
        "garage",
        "garage, hangar, workshop, repair, repairs, repairing, fixing the bike, "
        "fix the bike, fixing your bike, working on the bike, working on your bike, "
        "tuning the bike, bike maintenance, maintenance, mechanic, wrench, oil change, "
        "engine",
        "hangar_coveralls",
    ),
    _scene(
        "ceremony",
        "ceremony, parade, inspection, award ceremony, medal, medals, salute, saluting, "
        "honor guard, military ball",
        "dress_uniform",
    ),
    _scene(
        "formal",
        "gala, ballroom, banquet, black tie, black-tie, formal, formal dinner, "
        "fancy dinner, fancy restaurant, opera, commission, reception, cocktail party",
        "formal_commission",
        "dress_uniform",
    ),
    _scene(
        "tea",
        "tea time, tea counter, barista, racing calm, behind the counter, serving tea, "
        "pouring tea",
        "tea_counter",
    ),
    _scene(
        "stargazing",
        "stargazing, stargaze, stars, starry, starry sky, night sky, meteor, meteors, "
        "meteor shower, shooting star, telescope, milky way, constellation, constellations",
        "stargazing_night",
    ),
    _scene(
        "rain",
        "rain, raining, rainy, drizzle, storm, stormy, thunderstorm, downpour, umbrella, "
        "puddle, puddles",
        "rain_shell",
    ),
    _scene(
        "snow",
        "snow, snowing, snowy, snowfall, snowflakes, snowman, blizzard, winter, wintry, "
        "freezing, frost, frosty, cold weather, in the cold, cold night, cold day, "
        "ice skating",
        "winter_patrol",
    ),
    _scene(
        "powersports",
        "powersports, motor show, motorcycle show, bike show, auto show, expo, race track, "
        "racetrack, race, racing, grand prix, paddock, pit lane",
        "paddock_pass",
    ),
    _scene(
        "ride",
        "motorcycle, motorbike, bike ride, on your bike, on the bike, on her bike, ride, "
        "riding, joyride, road trip, back seat, backseat, highway, night ride",
        "night_ride",
        "speed_star",
        "astral_luminous",
    ),
    _scene(
        "cat_cafe",
        "cat cafe, cat café, cats, kitten, kittens, kitty, petting a cat, with a cat, "
        "holding a cat",
        "cat_cafe_date",
    ),
    _scene(
        "date",
        "date, date night, first date, movie, movies, cinema, movie theater",
        "cat_cafe_date",
        "off_duty_cap",
    ),
    _scene(
        "cafe",
        "cafe, café, coffee shop, errand, errands, shopping, groceries, grocery, "
        "supermarket, mall, market, bookstore, convenience store, downtown, city walk",
        "off_duty_cap",
    ),
    _scene(
        "reading",
        "reading, read a book, book, books, e-reader, library, sketchbook, "
        "drawing tablet, stylus, doodling, studying",
        "drawing_night",
    ),
)


_PATTERNS: tuple[tuple[Scene, re.Pattern[str]], ...] = tuple(
    (
        s,
        re.compile(
            r"\b(?:"
            + "|".join(re.escape(k) for k in sorted(s.keywords, key=len, reverse=True))
            + r")\b"
        ),
    )
    for s in SCENES
)
_BY_KEY = {s.key: s for s in SCENES}


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split())


def scene_of(text: str | None) -> str | None:
    """The scene ``text`` clearly sets (first by priority), or None."""
    if not text:
        return None
    lower = _normalize(text)
    for scene, pattern in _PATTERNS:
        if pattern.search(lower):
            return scene.key
    return None


def eligible(o: Outfit | None, level: int) -> bool:
    """Owned, drawable and acknowledged at ``level`` — fail closed."""
    return (
        o is not None
        and bool(o.image_tags)
        and not o.deny
        and level >= o.unlock_level
        and level >= o.visible_at
    )


def scene_outfit(
    scene: str | None,
    level: int,
    *,
    current: str | None = None,
    cat: dict[str, Outfit] | None = None,
    rng: random.Random | None = None,
) -> str | None:
    """The outfit she wears for ``scene`` at ``level`` → its id, or None to
    keep ``current`` (no scene, already dressed for it, or nothing unlocked)."""
    s = _BY_KEY.get(scene or "")
    if s is None:
        return None
    cat = catalog() if cat is None else cat
    if canonical_id(current, cat) in s.outfits:
        return None  # already in the kit for it — riding in Astral Luminous stays Astral Luminous
    options = [oid for oid in s.outfits if eligible(cat.get(oid), level)]
    if not options:
        return None
    return (
        options[0] if len(options) == 1 else (rng or random.Random(0)).choice(options)
    )


def dress_for_scene(
    text: str | None,
    level: int,
    *,
    current: str | None = None,
    cat: dict[str, Outfit] | None = None,
    rng: random.Random | None = None,
) -> str | None:
    """``scene_outfit(scene_of(text), ...)``."""
    return scene_outfit(scene_of(text), level, current=current, cat=cat, rng=rng)
