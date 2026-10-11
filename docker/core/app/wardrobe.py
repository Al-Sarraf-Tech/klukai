"""Klukai's wardrobe — one catalog, and one outfit at a time.

The catalog is ``costumes:`` in config/personality.yaml (hot-reloaded on
mtime). Every surface reads it through this module: the system prompt (today's
outfit + the wardrobe block), image generation (``image_tags``), the unlock
gates, and the PWA picker (/api/outfits). The pure catalog/pick layer lives in
app/wardrobe_catalog.py and is re-exported here.

She dresses herself. Each local day she picks a base outfit for herself —
pure and deterministic from weather, occasions, weekday, mood, his favourite
and novelty — and the duty roster (app/her_day.py) can put her in block kit
(coveralls at the hangar). His requests are requests: the outcome is decided
HERE, deterministically, never by the LLM — only a granted request changes
what she wears, so no prompt can talk her into a locked outfit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from .personality.loader import load_personality
from .proactive.state import now_local
from .wardrobe_catalog import (
    DEFAULT_OUTFIT, DEFAULT_REASON, Outfit, canonical_id, catalog, is_unlocked, occasions_on, pick_daily_outfit,
)
# The rest of the pure layer, re-exported (the redundant ``as`` marks it public):
# every surface reads the wardrobe through this module and tests patch it here.
from .wardrobe_catalog import (
    FALLBACK as FALLBACK, LEGACY_ALIASES as LEGACY_ALIASES, _weather_ok as _weather_ok,
    image_tags as image_tags, is_visible as is_visible, layer_note as layer_note, lookup as lookup,
    outfit_line as outfit_line,
)

logger = logging.getLogger(__name__)

MAX_REQUESTS_PER_DAY = 2
# Her day turns over at 0500, not midnight: the insomnia hours belong to the
# night before, and a 00:40 message must not pick tomorrow's clothes from a
# 2 °C night reading.
DAY_START_HOUR = 5
_WEATHER_TIMEOUT_S = 2.5
_FAILURE_RETRY_S = 300.0


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


async def warm_today(user_id: str, *, mood: str | None = None) -> None:
    """On connect: pick today's outfit in the background (never raises).

    ``mood`` is the session's mood, the same one the chat turn passes — this
    pick usually wins the race, so without it her mood would never count."""
    from .context import affection

    try:
        level = (await affection.get_state(user_id)).level
        await ensure_today(user_id, level, mood=mood)
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
# ("Klukai, wear...", "please put on..."), or you-led with a modal ("can you",
# "would you", "I want you to", "you should"). A bare "you" is not a request:
# "I like it when you wear the hoodie", "if you wear the coat you'll be fine"
# and "I'm going to wear a coat" are about her habits or about him.
_ADDRESSED = re.compile(
    r"(?:^|[.!?;,:]\s*|\b(?:please|just|now|go|and|so|then|ok|okay|hey)\s+)$"
    r"|\b(?:can|could|would|will|might|should)\s+you\s+(?:please\s+|maybe\s+|just\s+)?$"
    r"|\byou(?:'d| would| could| can| will| might| should| to)\s+(?:please\s+|maybe\s+|just\s+)?$"
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
    """Today's occasions for a request gate: those of her day (``day``) and of
    the calendar day — between midnight and 0500 on his birthday the rituals
    already greet him, so the oath must not answer "not today"."""
    from .rituals import get_birthday

    bday = await get_birthday(user_id)
    return occasions_on(day, None, bday) | occasions_on(now_local().date(), None, bday)


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
