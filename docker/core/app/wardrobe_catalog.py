"""Canon catalog, outfit gates and deterministic daily selection."""

from __future__ import annotations

import calendar
import hashlib
import random
from dataclasses import dataclass
from datetime import date
from typing import Any

from .personality.loader import load_personality

DEFAULT_OUTFIT = "blazing_star"
DEFAULT_REASON = "Standard operational gear."

# Ids the PWA/fact store used before the catalog was unified. Both were
# invented: midnight_sovereign (a "formal midnight gown") becomes her original
# formal wear; starlit_vow was the wedding gown, whose canon name is Indigo Oath.
LEGACY_ALIASES = {
    "midnight_sovereign": "formal_commission",
    "starlit_vow": "indigo_oath",
}

MAX_REQUESTS_PER_DAY = 2
# Her day turns over at 0500, not midnight: the insomnia hours belong to the
# night before, and a 00:40 message must not pick tomorrow's clothes from a
# 2 °C night reading.
DAY_START_HOUR = 5
_WEATHER_TIMEOUT_S = 2.5
_FAILURE_RETRY_S = 300.0

# Rough monthly daytime temperature for the Commander's city, used only when
# the live weather fetch fails — enough to keep a coat off her in July.
_SEASONAL_TEMP_C = (0, 2, 8, 15, 21, 27, 29, 28, 24, 17, 9, 2)

_OCCASION_REASONS = {
    "new_year": "New Year's Day.",
    "white_day": "White Day.",
    "day_416": "416 Day — her own day.",
    "starwish_cn": "The anniversary of her first Starwish win.",
    "starwish_global": "The anniversary of her Global Starwish win.",
    "halloween": "It's Halloween.",
    "christmas": "It's the holidays.",
    "commander_birthday": "It's his birthday.",
}


@dataclass(frozen=True)
class Outfit:
    id: str
    name: str
    source: str
    category: str
    unlock_level: int
    description: str
    image_tags: str
    visible_at: int
    open_at: int
    deny: bool
    daily: dict[str, Any]
    occasions: tuple[str, ...]
    aliases: tuple[str, ...]
    stance: str
    # Per-render variety (app/wardrobe_variation.py). None = her category's
    # default pool; () = fixed (the gown is always hair down, the oath bare).
    hair: tuple[str, ...] | None = None
    accessories: tuple[str, ...] | None = None

    @property
    def blurb(self) -> str:
        """First sentence of the description (split on '. ' so "No.1" survives)."""
        return self.description.split(". ")[0].strip().rstrip(".")


def _optional_list(raw: dict[str, Any], key: str) -> tuple[str, ...] | None:
    """A YAML list as a tuple; absent/null stays None (use the default)."""
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _as_outfit(oid: str, raw: dict[str, Any]) -> Outfit:
    private = raw.get("private") or [0, 0]
    return Outfit(
        id=oid,
        name=str(raw.get("name") or oid.replace("_", " ").title()),
        source=str(raw.get("source", "canon")),
        category=str(raw.get("category", "duty")),
        unlock_level=int(raw.get("unlock_level", 0)),
        description=" ".join(str(raw.get("description", "")).split()),
        image_tags=str(raw.get("image_tags", "")),
        visible_at=int(private[0]),
        open_at=int(private[1]),
        deny=bool(raw.get("deny", False)),
        daily=dict(raw.get("daily") or {}),
        occasions=tuple(raw.get("occasions") or ()),
        aliases=tuple(str(a).lower() for a in raw.get("aliases") or ()),
        stance=str(raw.get("stance", "")),
        hair=_optional_list(raw, "hair"),
        accessories=_optional_list(raw, "accessories"),
    )


# Holds the personality dict itself (not its id()) so a hot reload can never
# alias a freed dict's address onto a stale catalog.
_catalog_cache: tuple[dict[str, Any], dict[str, Outfit]] | None = None


def catalog(p: dict[str, Any] | None = None) -> dict[str, Outfit]:
    """The wardrobe, keyed by id. Cached per loaded personality object."""
    global _catalog_cache
    p = load_personality() if p is None else p
    if _catalog_cache is not None and _catalog_cache[0] is p:
        return _catalog_cache[1]
    out = {
        oid: _as_outfit(oid, raw)
        for oid, raw in (p.get("costumes") or {}).items()
        if isinstance(raw, dict)
    }
    _catalog_cache = (p, out)
    return out


def canonical_id(raw: str | None, cat: dict[str, Outfit] | None = None) -> str | None:
    """Normalize a stored/requested id; legacy ids map forward. Unknown → None."""
    if not raw:
        return None
    cat = catalog() if cat is None else cat
    oid = LEGACY_ALIASES.get(raw.strip().lower(), raw.strip().lower())
    return oid if oid in cat else None


def is_unlocked(
    oid: str | None, level: int, cat: dict[str, Outfit] | None = None
) -> bool:
    """Fail-closed: an unknown id is locked."""
    cat = catalog() if cat is None else cat
    o = cat.get(canonical_id(oid, cat) or "")
    return o is not None and level >= o.unlock_level


def is_visible(o: Outfit, level: int) -> bool:
    """Whether she acknowledges owning it at this level (UI listing)."""
    if o.deny:
        return level >= o.unlock_level
    return level >= o.visible_at


FALLBACK = Outfit(
    id=DEFAULT_OUTFIT,
    name="Blazing Star",
    source="canon",
    category="duty",
    unlock_level=0,
    description=DEFAULT_REASON,
    image_tags="",
    visible_at=0,
    open_at=0,
    deny=False,
    daily={},
    occasions=(),
    aliases=(),
    stance="",
)


def lookup(oid: str | None, cat: dict[str, Outfit] | None = None) -> Outfit:
    """The catalog entry for ``oid`` — or a tagless Blazing Star stand-in."""
    cat = catalog() if cat is None else cat
    return cat.get(canonical_id(oid, cat) or "") or FALLBACK


def image_tags(oid: str | None, cat: dict[str, Outfit] | None = None) -> str | None:
    cat = catalog() if cat is None else cat
    o = cat.get(canonical_id(oid, cat) or "")
    return o.image_tags if o and o.image_tags else None


_CONDITION_REASONS = {
    "rain": "It's raining.",
    "drizzle": "It's drizzling.",
    "storm": "There's a storm.",
    "snow": "It's snowing.",
    "clear": "Clear skies.",
    "cloudy": "Overcast.",
    "fog": "Fog.",
}


# ── Occasions ──────────────────────────────────────────────────────────────


def _mmdd_matches(spec: str, mmdd: str) -> bool:
    if ".." not in spec:
        return spec == mmdd
    start, end = (s.strip() for s in spec.split("..", 1))
    if start <= end:
        return start <= mmdd <= end
    return mmdd >= start or mmdd <= end  # range wraps the new year


def occasions_on(
    day: date, p: dict[str, Any] | None = None, commander_birthday: str | None = None
) -> frozenset[str]:
    p = load_personality() if p is None else p
    mmdd = day.strftime("%m-%d")
    found = {
        key
        for key, specs in (p.get("wardrobe_occasions") or {}).items()
        if any(_mmdd_matches(str(s), mmdd) for s in specs or ())
    }
    # A Feb 29 birthday is kept on Feb 28 in common years (as rituals does).
    if commander_birthday == "02-29" and not calendar.isleap(day.year):
        commander_birthday = "02-28"
    if commander_birthday and commander_birthday == mmdd:
        found.add("commander_birthday")
    return frozenset(found)


# ── The daily pick (pure) ──────────────────────────────────────────────────


def _rng(seed: str, day: date) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{day.isoformat()}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _weather_ok(daily: dict[str, Any], temp: float, condition: str | None) -> bool:
    if "temp_min" in daily and temp < daily["temp_min"]:
        return False
    if "temp_max" in daily and temp > daily["temp_max"]:
        return False
    if daily.get("conditions") and condition not in daily["conditions"]:
        return False
    return condition not in (daily.get("avoid_conditions") or ())


def _daily_score(
    o: Outfit,
    day: date,
    temp: float,
    condition: str | None,
    hit: list[str],
    mood: str | None,
    favorite: str | None,
    recent: tuple[str, ...] | list[str],
) -> tuple[float, str]:
    daily = o.daily
    score = float(daily.get("weight", 1.0))
    reasons: list[tuple[float, str]] = []
    if hit:
        # +20 for an occasion, +5 more when it is his birthday: that outranks
        # a holiday it falls inside (it was a coin flip).
        score += 20.0
        reason = _OCCASION_REASONS.get(hit[0], "An occasion.")
        if "commander_birthday" in hit:
            score += 5.0
            reason = _OCCASION_REASONS["commander_birthday"]
        reasons.append((20.0, reason))
    if daily.get("conditions"):
        score += 2.0
        reasons.append((2.0, _CONDITION_REASONS.get(str(condition), "The weather.")))
    if daily.get("temp_max", 99) <= 5 or daily.get("temp_min", -99) >= 25:
        score += 1.0
        reasons.append((2.5, f"It's {round(temp)}°C."))
    if day.weekday() in (daily.get("weekdays") or ()):
        score += 1.5
        reasons.append(
            (1.5, "A duty day." if o.category == "duty" else "Off-duty plans.")
        )
    if mood and mood in (daily.get("moods") or ()):
        score += 1.0
        reasons.append((1.0, f"She's feeling {mood.replace('_', ' ')}."))
    if favorite == o.id:
        score += 1.5
        reasons.append((1.5, "He likes this one — not that it was a factor."))
    if o.category != "duty" and o.id in recent:
        score -= 2.0 if recent[0] == o.id else 1.0
    reason = max(reasons)[1] if reasons else "Her call."
    return score, reason


def pick_daily_outfit(
    cat: dict[str, Outfit],
    day: date,
    level: int,
    *,
    weather: dict[str, Any] | None = None,
    mood: str | None = None,
    favorite: str | None = None,
    recent: tuple[str, ...] | list[str] = (),
    occasions: frozenset[str] = frozenset(),
    seed: str = "",
) -> tuple[str, str]:
    """Choose what she wears today → (outfit_id, reason). Same inputs, same answer."""
    rng = _rng(seed, day)
    live = (weather or {}).get("temp_c")
    temp = float(live) if live is not None else float(_SEASONAL_TEMP_C[day.month - 1])
    condition = (weather or {}).get("condition")
    best: tuple[float, str, str] | None = None
    for oid in sorted(cat):
        o = cat[oid]
        hit = sorted(occasions & set(o.occasions))
        daily = o.daily
        if level < max(o.unlock_level, int(daily.get("min_level", 0))):
            continue
        if not hit and (not daily or not _weather_ok(daily, temp, condition)):
            continue
        score, reason = _daily_score(
            o, day, temp, condition, hit, mood, favorite, recent
        )
        score += rng.random() * 1.5
        if best is None or score > best[0]:
            best = (score, oid, reason)
    if best is None:
        return DEFAULT_OUTFIT, DEFAULT_REASON
    return best[1], best[2]


def layer_note(o: Outfit, hour: int, level: int) -> str:
    """Time-of-day adjustments on top of the outfit (prompt only)."""
    if o.category in ("sleep", "swim", "oath"):
        return ""
    late = hour >= 21 or hour < 5
    if late and level >= 3 and "hair down" not in o.image_tags:
        return "hair down — the Commander sees a side others don't"
    if 17 <= hour < 21 and level >= 2 and o.category == "duty":
        return "jacket off, gear partially stowed"
    return ""


def outfit_line(o: Outfit, reason: str, source: str, hour: int, level: int) -> str:
    """The CURRENT OUTFIT line for the system prompt."""
    layer = layer_note(o, hour, level)
    line = f"{o.name} — {o.blurb}" + (f" ({layer})" if layer else "") + "."
    if source == "commander":
        return (
            line
            + " He asked for it and you agreed. If it comes up, it was your decision."
        )
    if source == "roster":
        return line + " Kit for what you're doing right now."
    return line + f" Why today: {reason}"
