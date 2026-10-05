"""Klukai's wardrobe — one catalog, and one outfit at a time.

The catalog is ``costumes:`` in config/personality.yaml (hot-reloaded on
mtime). Every surface reads it through this module: the system prompt (today's
outfit + the wardrobe block), image generation (``image_tags``), the unlock
gates, and the PWA picker (/api/outfits).

She dresses herself. Each local day she picks a base outfit for herself —
pure and deterministic from weather, occasions, weekday, mood, his favourite
and novelty — and the duty roster (app/her_day.py) can put her in block kit
(coveralls at the hangar). His requests are requests: the outcome is decided
HERE, deterministically, never by the LLM — only a granted request changes
what she wears, so no prompt can talk her into a locked outfit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from .personality.loader import load_personality
from .proactive.state import now_local

logger = logging.getLogger(__name__)

DEFAULT_OUTFIT = "blazing_star"
DEFAULT_REASON = "Standard operational gear."

# Ids the PWA/fact store used before the catalog was unified. Both were
# invented: midnight_sovereign (a "formal midnight gown") becomes her original
# formal wear; starlit_vow was the wedding gown, whose canon name is Indigo Oath.
LEGACY_ALIASES = {"midnight_sovereign": "formal_commission", "starlit_vow": "indigo_oath"}

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
    daily: dict
    occasions: tuple[str, ...]
    aliases: tuple[str, ...]
    stance: str

    @property
    def blurb(self) -> str:
        """First sentence of the description (split on '. ' so "No.1" survives)."""
        return self.description.split(". ")[0].strip().rstrip(".")


def _as_outfit(oid: str, raw: dict) -> Outfit:
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
    )


# Holds the personality dict itself (not its id()) so a hot reload can never
# alias a freed dict's address onto a stale catalog.
_catalog_cache: tuple[dict, dict[str, Outfit]] | None = None


def catalog(p: dict | None = None) -> dict[str, Outfit]:
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


def is_unlocked(oid: str | None, level: int, cat: dict[str, Outfit] | None = None) -> bool:
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
    id=DEFAULT_OUTFIT, name="Blazing Star", source="canon", category="duty", unlock_level=0,
    description=DEFAULT_REASON, image_tags="", visible_at=0, open_at=0, deny=False,
    daily={}, occasions=(), aliases=(), stance="",
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


def occasions_on(day: date, p: dict | None = None, commander_birthday: str | None = None) -> frozenset[str]:
    p = load_personality() if p is None else p
    mmdd = day.strftime("%m-%d")
    found = {
        key
        for key, specs in (p.get("wardrobe_occasions") or {}).items()
        if any(_mmdd_matches(str(s), mmdd) for s in specs or ())
    }
    if commander_birthday and commander_birthday == mmdd:
        found.add("commander_birthday")
    return frozenset(found)


# ── The daily pick (pure) ──────────────────────────────────────────────────


def _rng(seed: str, day: date) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{day.isoformat()}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _weather_ok(daily: dict, temp: float, condition: str | None) -> bool:
    if "temp_min" in daily and temp < daily["temp_min"]:
        return False
    if "temp_max" in daily and temp > daily["temp_max"]:
        return False
    if daily.get("conditions") and condition not in daily["conditions"]:
        return False
    return condition not in (daily.get("avoid_conditions") or ())


def pick_daily_outfit(
    cat: dict[str, Outfit],
    day: date,
    level: int,
    *,
    weather: dict | None = None,
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
        score = float(daily.get("weight", 1.0))
        reasons: list[tuple[float, str]] = []
        if hit:
            score += 20.0
            reasons.append((20.0, _OCCASION_REASONS.get(hit[0], "An occasion.")))
        if daily.get("conditions"):
            score += 2.0
            reasons.append((2.0, _CONDITION_REASONS.get(str(condition), "The weather.")))
        if daily.get("temp_max", 99) <= 5 or daily.get("temp_min", -99) >= 25:
            score += 1.0
            reasons.append((2.5, f"It's {round(temp)}°C."))
        if day.weekday() in (daily.get("weekdays") or ()):
            score += 1.5
            reasons.append((1.5, "A duty day." if o.category == "duty" else "Off-duty plans."))
        if mood and mood in (daily.get("moods") or ()):
            score += 1.0
            reasons.append((1.0, f"She's feeling {mood.replace('_', ' ')}."))
        if favorite == oid:
            score += 1.5
            reasons.append((1.5, "He likes this one — not that it was a factor."))
        if o.category != "duty" and oid in recent:
            score -= 2.0 if recent[0] == oid else 1.0
        score += rng.random() * 1.5
        reason = max(reasons)[1] if reasons else "Her call."
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
        return line + " He asked for it and you agreed. If it comes up, it was your decision."
    if source == "roster":
        return line + " Kit for what you're doing right now."
    return line + f" Why today: {reason}"


# ── Persistence (companion_her_day, migration 190) ──────────────────────────


@dataclass(frozen=True)
class HerDayRow:
    day: date
    base_outfit_id: str
    base_reason: str
    requested_outfit_id: str | None = None
    request_changes: int = 0
    weather: dict | None = None
    persisted: bool = True  # False = a fail-soft stand-in with no DB row behind it


def her_date(now: datetime) -> date:
    """The day she is living at ``now`` (turns over at DAY_START_HOUR)."""
    return (now - timedelta(hours=DAY_START_HOUR)).date()


_today_cache: dict[str, tuple[float, HerDayRow]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock(user_id: str) -> asyncio.Lock:
    """Serializes first-touch and request writes per Commander, so a slow
    first touch can never overwrite a request recorded meanwhile."""
    return _locks.setdefault(user_id, asyncio.Lock())


def clear_cache() -> None:
    _today_cache.clear()
    _locks.clear()


def _row_from_db(day: date, r: tuple) -> HerDayRow:
    weather = r[4]
    if isinstance(weather, str):
        weather = json.loads(weather)
    return HerDayRow(day, r[0], r[1], r[2], int(r[3] or 0), weather)


async def _load_row(user_id: str, day: date) -> HerDayRow | None:
    from .db import get_conn

    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT base_outfit_id, base_reason, requested_outfit_id, request_changes, weather "
            "FROM companion_her_day WHERE user_id = %s AND day = %s",
            (user_id, day),
        )
        r = await cur.fetchone()
    return _row_from_db(day, r) if r else None


async def _insert_row(user_id: str, row: HerDayRow) -> None:
    from .db import get_conn_autocommit

    async with get_conn_autocommit() as conn:
        await conn.execute(
            "INSERT INTO companion_her_day (user_id, day, base_outfit_id, base_reason, weather) "
            "VALUES (%s, %s, %s, %s, %s::jsonb) ON CONFLICT (user_id, day) DO NOTHING",
            (user_id, row.day, row.base_outfit_id, row.base_reason,
             json.dumps(row.weather) if row.weather is not None else None),
        )


async def recent_outfits(user_id: str, before: date, limit: int = 3) -> list[str]:
    from .db import get_conn

    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT COALESCE(requested_outfit_id, base_outfit_id) FROM companion_her_day "
            "WHERE user_id = %s AND day < %s ORDER BY day DESC LIMIT %s",
            (user_id, before, limit),
        )
        return [r[0] for r in await cur.fetchall()]


async def history(user_id: str, days: int = 30) -> list[dict]:
    """The wardrobe calendar: what she wore, newest first."""
    from .db import get_conn

    async with get_conn() as conn:
        cur = await conn.execute(
            "SELECT day, base_outfit_id, base_reason, requested_outfit_id FROM companion_her_day "
            "WHERE user_id = %s ORDER BY day DESC LIMIT %s",
            (user_id, max(1, min(days, 400))),
        )
        rows = await cur.fetchall()
    return [
        {
            "day": r[0].isoformat(),
            "outfit_id": canonical_id(r[3] or r[1]) or DEFAULT_OUTFIT,
            "reason": r[2],
            "requested": r[3] is not None,
        }
        for r in rows
    ]


async def favorite_outfit(user_id: str) -> str | None:
    """His pick in the PWA wardrobe (fact ``costume``) — migrated forward on read."""
    from .context import memory

    raw = await memory.recall_fact("costume", user_id=user_id)
    oid = canonical_id(raw)
    if raw and oid and oid != raw:
        # Data migration: rewrite legacy ids (midnight_sovereign, starlit_vow).
        await memory.store_fact("costume", oid, user_id=user_id)
    return oid


async def decided_outfit(user_id: str, day: date, level: int, cat: dict[str, Outfit]) -> str | None:
    """An outfit he chose for today through a Command Decision (fact
    ``outfit:tomorrow`` = {"date": ISO day, "outfit_id": id}), if still wearable."""
    from .context import memory

    raw = await memory.recall_fact("outfit:tomorrow", user_id=user_id)
    try:
        data = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("date") != day.isoformat():
        return None
    oid = canonical_id(data.get("outfit_id"), cat)
    return oid if oid and is_unlocked(oid, level, cat) else None


async def _quick_weather() -> dict | None:
    from .weather_client import fetch_weather

    try:
        return await asyncio.wait_for(fetch_weather(), _WEATHER_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError):
        return None


async def ensure_today(user_id: str, level: int, *, mood: str | None = None) -> HerDayRow:
    """Today's row, picking (and persisting) her outfit on first touch of the day.

    Fail-soft: on any DB error she wears Blazing Star (``persisted=False``) and
    the DB is retried after 5 minutes.
    """
    day = her_date(now_local())
    async with _lock(user_id):
        cached = _today_cache.get(user_id)
        if cached and cached[1].day == day and (
            cached[1].persisted or time.monotonic() - cached[0] < _FAILURE_RETRY_S
        ):
            return cached[1]
        try:
            row = await _load_row(user_id, day)
            if row is None:
                from .rituals import get_birthday

                p = load_personality()
                cat = catalog(p)
                weather = await _quick_weather()
                decided = await decided_outfit(user_id, day, level, cat)
                if decided:
                    oid, reason = decided, "His call — she asked, he decided."
                else:
                    oid, reason = pick_daily_outfit(
                        cat, day, level,
                        weather=weather,
                        mood=mood,
                        favorite=await favorite_outfit(user_id),
                        recent=await recent_outfits(user_id, day),
                        occasions=occasions_on(day, p, await get_birthday(user_id)),
                        seed=user_id,
                    )
                await _insert_row(user_id, HerDayRow(day, oid, reason, weather=weather))
                # Re-read: the row actually stored (a concurrent writer may have won).
                row = await _load_row(user_id, day)
                if row is None:
                    raise RuntimeError("her day row missing after insert")
        except Exception as e:
            logger.warning("Her Day unavailable, defaulting outfit: %s", e)
            row = HerDayRow(day, DEFAULT_OUTFIT, DEFAULT_REASON, persisted=False)
        _today_cache[user_id] = (time.monotonic(), row)
        return row


async def warm_today(user_id: str) -> None:
    """On connect: pick today's outfit in the background (never raises)."""
    from .context import affection

    try:
        level = (await affection.get_state(user_id)).level
        await ensure_today(user_id, level)
        _prewarm_portrait(user_id, level)
    except Exception as e:
        logger.debug("Her Day warm-up skipped: %s", e)


def _prewarm_portrait(user_id: str, level: int) -> None:
    """Today's outfit was picked or changed: start drawing her portrait in it."""
    from . import portrait

    portrait.prewarm_soon(user_id, level)


# ── His requests ───────────────────────────────────────────────────────────

_VERBS = (
    r"wear|put on|change into|change in to|switch into|switch to|dress in|"
    r"dress up in|try on|slip into"
)
# Phrasings with "you" built in: "I want to see you in the maid outfit".
_YOU_IN = r"(?:see|want|need) you in"
_NEGATION = re.compile(
    r"\b(?:don'?t|do not|never|stop|quit|no more|not|can'?t|cannot|won'?t|shouldn'?t|"
    r"mustn'?t|wouldn'?t|couldn'?t|didn'?t|doesn'?t)\s+(?:\w+\s+){0,2}$"
)
_QUESTION_ABOUT = re.compile(r"\b(?:do|did|have) you (?:ever |still |even )?$")
# The wear-verb must be addressed to HER: an imperative opening a clause
# ("Klukai, wear...", "please put on..."), or you-led ("can you", "would you",
# "I want you to", "you should"). "I'm going to wear a coat" is about him.
_ADDRESSED = re.compile(
    r"(?:^|[.!?;,:]\s*|\b(?:please|just|now|go|and|so|then|ok|okay|hey)\s+)$"
    r"|\byou(?:'d| would| could| can| will| might| should| to)?\s+(?:please\s+|maybe\s+|just\s+)?$"
)


def detect_outfit_request(message: str, cat: dict[str, Outfit] | None = None) -> str | None:
    """A request for HER to change into a named outfit → its id, else None.

    High precision on purpose — every hit is a real decision (a change, a
    counted request, or a refusal he never asked for). Needs a wear-verb
    addressed to her, a catalog alias in the same sentence, and no negation
    or idle question ("don't wear...", "do you ever wear...").
    """
    cat = catalog() if cat is None else cat
    lower = " ".join(message.lower().split())
    aliases = [(alias, o.id) for o in cat.values() for alias in (*o.aliases, o.name.lower())]
    for verb in re.finditer(rf"\b(?:{_YOU_IN}|{_VERBS})\b", lower):
        before = lower[: verb.start()]
        if _NEGATION.search(before) or _QUESTION_ABOUT.search(before):
            continue
        if not verb.group(0).endswith("you in") and not _ADDRESSED.search(before):
            continue
        tail = re.split(r"[.!?;]", lower[verb.end(): verb.end() + 48], maxsplit=1)[0]
        # The outfit named FIRST after the verb wins ("wear the coat over the
        # maid uniform" is the coat); at the same spot, the longest alias.
        hits = [
            (m.start(), -len(alias), oid)
            for alias, oid in aliases
            if (m := re.search(rf"\b{re.escape(alias)}\b", tail))
        ]
        if hits:
            return min(hits)[2]
    return None


@dataclass(frozen=True)
class RequestOutcome:
    outfit_id: str
    decision: str  # comply | already | locked | refuse | unacknowledged | deny | not_today | not_now | occasion_only | limit
    note: str
    # She won't discuss her wardrobe at all: the prompt must not even name it.
    discreet: bool = False


def _band_note(level: int) -> str:
    if level >= 7:
        return "Comply without fuss; let him see — briefly — that you're pleased he asked."
    if level >= 5:
        return "Comply; you may pretend you were already planning to."
    if level >= 3:
        return "Comply, but make clear it's your decision, not an order he gave you. You may bill it."
    return "Comply, curtly, for practical reasons only."


# Days that "matter" enough for the wedding gown below the oath: its own
# occasions (White Day) and his birthday — not Halloween or Christmas week.
_OATH_DAYS = frozenset({"commander_birthday"})


def decide_request(
    o: Outfit,
    level: int,
    *,
    current_id: str,
    request_changes: int,
    occasions_today: frozenset[str],
    hour: int,
    deployed: bool = False,
    ui: bool = False,
) -> RequestOutcome:
    """Deterministic outcome of 'wear X'. The LLM only voices it.

    ``ui=True`` is the PWA wardrobe button: the unlock level already is the
    permission there, so the low-affection reticence and the daily cap don't
    apply — but the onesie, the oath, the seasons and the hour still do.
    """
    if deployed:
        return RequestOutcome(o.id, "not_now", "You're deployed. Not now — the mission kit stays on.")
    if o.deny:
        return RequestOutcome(o.id, "deny", "Deny the thing exists. Flatly. Change the subject.", discreet=True)
    if level < o.visible_at:
        return RequestOutcome(o.id, "unacknowledged", "Do not acknowledge owning any such outfit.", discreet=True)
    if o.id == current_id:
        return RequestOutcome(o.id, "already", "You are already wearing it. Note that he didn't notice.")
    if level < o.unlock_level:
        hint = (
            "Refuse without discussing your wardrobe at all." if level < 3
            else "Refuse: 'Not yet.' Nothing more." if level < 5
            else "Refuse, but hint — in your voice — that it's a matter of trust, not time."
        )
        return RequestOutcome(o.id, "locked", hint, discreet=level < 3)
    if not ui and level < 3 and o.category not in ("duty", "weather", "training"):
        return RequestOutcome(
            o.id, "refuse", "'My attire is not a topic for discussion, Commander.'", discreet=True,
        )
    if o.category == "oath" and level < 9 and not occasions_today & (_OATH_DAYS | set(o.occasions)):
        return RequestOutcome(o.id, "not_today", "'...Not today. Ask me on a day that matters.'")
    if o.category == "seasonal" and not (occasions_today & set(o.occasions)):
        return RequestOutcome(o.id, "occasion_only", "Wrong season for it. Say so, dryly.")
    if o.category == "sleep" and 5 <= hour < 21:
        return RequestOutcome(o.id, "not_now", "Late-watch clothes are not for daytime. Decline.")
    if not ui and request_changes >= MAX_REQUESTS_PER_DAY:
        return RequestOutcome(o.id, "limit", "'I am not a mannequin, Commander.' You do not change again today.")
    return RequestOutcome(o.id, "comply", _band_note(level))


async def _record_request(user_id: str, day: date, oid: str, *, count: bool = True) -> bool:
    """Persist his outfit for today. A counted request only lands while under
    the daily cap (atomic — two devices can't sneak a third change through).
    Returns whether a row was updated."""
    from .db import get_conn_autocommit

    async with get_conn_autocommit() as conn:
        cur = await conn.execute(
            "UPDATE companion_her_day SET requested_outfit_id = %s, "
            "request_changes = request_changes + %s, updated_at = NOW() "
            "WHERE user_id = %s AND day = %s AND (%s = 0 OR request_changes < %s)",
            (oid, 1 if count else 0, user_id, day, 1 if count else 0, MAX_REQUESTS_PER_DAY),
        )
        return bool(cur.rowcount)


async def set_requested(user_id: str, oid: str, level: int) -> None:
    """His pick from the PWA wardrobe: she wears it for the rest of today.

    The dorm "change outfit" button, as in GFL2 — gated by ``decide_request``
    in UI mode by the caller, and never counted toward the daily request cap.
    Raises if it could not be persisted.
    """
    row = await ensure_today(user_id, level)
    async with _lock(user_id):
        if not row.persisted or not await _record_request(user_id, row.day, oid, count=False):
            raise RuntimeError("her day not persisted; outfit not changed")
        _today_cache[user_id] = (time.monotonic(), replace(row, requested_outfit_id=oid))
    _prewarm_portrait(user_id, level)


async def occasions_for(user_id: str, day: date) -> frozenset[str]:
    from .rituals import get_birthday

    return occasions_on(day, None, await get_birthday(user_id))


async def handle_request(
    user_id: str, oid: str, level: int, *, current_id: str,
    mood: str | None = None, deployed: bool = False,
) -> RequestOutcome:
    """Decide his request and, if she agrees, make it so for the rest of today."""
    cat = catalog()
    row = await ensure_today(user_id, level, mood=mood)
    outcome = decide_request(
        cat[oid], level,
        current_id=current_id,
        request_changes=row.request_changes,
        occasions_today=await occasions_for(user_id, row.day),
        hour=now_local().hour,
        deployed=deployed,
    )
    if outcome.decision != "comply":
        return outcome
    async with _lock(user_id):
        try:
            recorded = row.persisted and await _record_request(user_id, row.day, oid)
        except Exception as e:
            logger.warning("Could not record outfit request: %s", e)
            recorded = False
        if not recorded:
            return RequestOutcome(oid, "refuse", "Something came up; you don't change right now. Don't explain.")
        _today_cache[user_id] = (
            time.monotonic(),
            replace(row, requested_outfit_id=oid, request_changes=row.request_changes + 1),
        )
    _prewarm_portrait(user_id, level)
    return outcome


def request_block(outcome: RequestOutcome, cat: dict[str, Outfit] | None = None) -> str:
    """Per-message prompt block voicing a decided request."""
    cat = catalog() if cat is None else cat
    o = cat[outcome.outfit_id]
    granted = outcome.decision == "comply"
    state = (
        f"You agreed: you are now wearing {o.name}."
        if granted
        else "You are NOT changing. Do not describe changing, and do not promise to later."
    )
    if outcome.discreet:
        # Below the bond where she discusses her wardrobe: no name, no stance —
        # nothing for the reply to echo back.
        return (
            "WARDROBE REQUEST: The Commander asked you to change clothes.\n"
            f"OUTCOME (already decided — do not change it): {state}\n"
            f"Play it: {outcome.note} Do not name or describe any outfit.\n"
            "Keep it to a sentence, then carry on with the conversation."
        )
    stance = f"\nHow you see it: {o.stance}" if o.stance else ""
    return (
        f"WARDROBE REQUEST: The Commander asked you to wear {o.name}.\n"
        f"OUTCOME (already decided — do not change it): {state}\n"
        f"Play it: {outcome.note}{stance}\n"
        "Keep it to a sentence or two, then carry on with the conversation."
    )


def outfit_payload(o: Outfit, *, reason: str, source: str, level: int) -> dict:
    return {
        "id": o.id,
        "name": o.name,
        "blurb": o.blurb,
        "category": o.category,
        "source": source,
        "reason": reason if source == "auto" else "",
        "canon": o.source == "canon",
        "level_ok": level >= o.unlock_level,
    }

