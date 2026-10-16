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
from typing import Any


import asyncio
import json
import logging
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from .personality.loader import load_personality
from .proactive.state import now_local

from .wardrobe_catalog import (
    DEFAULT_OUTFIT as DEFAULT_OUTFIT,
    DEFAULT_REASON as DEFAULT_REASON,
    LEGACY_ALIASES as LEGACY_ALIASES,
    MAX_REQUESTS_PER_DAY as MAX_REQUESTS_PER_DAY,
    DAY_START_HOUR as DAY_START_HOUR,
    _WEATHER_TIMEOUT_S as _WEATHER_TIMEOUT_S,
    _FAILURE_RETRY_S as _FAILURE_RETRY_S,
    _SEASONAL_TEMP_C as _SEASONAL_TEMP_C,
    _OCCASION_REASONS as _OCCASION_REASONS,
    Outfit as Outfit,
    _as_outfit as _as_outfit,
    _catalog_cache as _catalog_cache,
    catalog as catalog,
    canonical_id as canonical_id,
    is_unlocked as is_unlocked,
    is_visible as is_visible,
    FALLBACK as FALLBACK,
    lookup as lookup,
    image_tags as image_tags,
    _CONDITION_REASONS as _CONDITION_REASONS,
    _mmdd_matches as _mmdd_matches,
    occasions_on as occasions_on,
    _rng as _rng,
    _weather_ok as _weather_ok,
    pick_daily_outfit as pick_daily_outfit,
    layer_note as layer_note,
    outfit_line as outfit_line,
)


from .wardrobe_requests import (
    request_block as request_block,
    _VERBS as _VERBS,
    _YOU_IN as _YOU_IN,
    _NEGATION as _NEGATION,
    _QUESTION_ABOUT as _QUESTION_ABOUT,
    _ADDRESSED as _ADDRESSED,
    detect_outfit_request as detect_outfit_request,
    RequestOutcome as RequestOutcome,
    _band_note as _band_note,
    _OATH_DAYS as _OATH_DAYS,
    decide_request as decide_request,
)


logger = logging.getLogger(__name__)

# ── Persistence (companion_her_day, migration 190) ──────────────────────────


@dataclass(frozen=True)
class HerDayRow:
    day: date
    base_outfit_id: str
    base_reason: str
    requested_outfit_id: str | None = None
    request_changes: int = 0
    weather: dict[str, Any] | None = None
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


def _row_from_db(day: date, r: tuple[Any, ...]) -> HerDayRow:
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
            (
                user_id,
                row.day,
                row.base_outfit_id,
                row.base_reason,
                json.dumps(row.weather) if row.weather is not None else None,
            ),
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


async def history(user_id: str, days: int = 30) -> list[dict[str, Any]]:
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


async def decided_outfit(
    user_id: str, day: date, level: int, cat: dict[str, Outfit]
) -> str | None:
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


async def _quick_weather() -> dict[str, Any] | None:
    from .weather_client import fetch_weather

    try:
        return await asyncio.wait_for(fetch_weather(), _WEATHER_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError):
        return None


async def ensure_today(
    user_id: str, level: int, *, mood: str | None = None
) -> HerDayRow:
    """Today's row, picking (and persisting) her outfit on first touch of the day.

    Fail-soft: on any DB error she wears Blazing Star (``persisted=False``) and
    the DB is retried after 5 minutes.
    """
    day = her_date(now_local())
    async with _lock(user_id):
        cached = _today_cache.get(user_id)
        if (
            cached
            and cached[1].day == day
            and (cached[1].persisted or time.monotonic() - cached[0] < _FAILURE_RETRY_S)
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
                        cat,
                        day,
                        level,
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


async def _record_request(
    user_id: str, day: date, oid: str, *, count: bool = True
) -> bool:
    """Persist his outfit for today. A counted request only lands while under
    the daily cap (atomic — two devices can't sneak a third change through).
    Returns whether a row was updated."""
    from .db import get_conn_autocommit

    async with get_conn_autocommit() as conn:
        cur = await conn.execute(
            "UPDATE companion_her_day SET requested_outfit_id = %s, "
            "request_changes = request_changes + %s, updated_at = NOW() "
            "WHERE user_id = %s AND day = %s AND (%s = 0 OR request_changes < %s)",
            (
                oid,
                1 if count else 0,
                user_id,
                day,
                1 if count else 0,
                MAX_REQUESTS_PER_DAY,
            ),
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
        if not row.persisted or not await _record_request(
            user_id, row.day, oid, count=False
        ):
            raise RuntimeError("her day not persisted; outfit not changed")
        _today_cache[user_id] = (
            time.monotonic(),
            replace(row, requested_outfit_id=oid),
        )
    _prewarm_portrait(user_id, level)


async def occasions_for(user_id: str, day: date) -> frozenset[str]:
    from .rituals import get_birthday

    return occasions_on(day, None, await get_birthday(user_id))


async def handle_request(
    user_id: str,
    oid: str,
    level: int,
    *,
    current_id: str,
    mood: str | None = None,
    deployed: bool = False,
) -> RequestOutcome:
    """Decide his request and, if she agrees, make it so for the rest of today."""
    cat = catalog()
    row = await ensure_today(user_id, level, mood=mood)
    outcome = decide_request(
        cat[oid],
        level,
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
            return RequestOutcome(
                oid,
                "refuse",
                "Something came up; you don't change right now. Don't explain.",
            )
        _today_cache[user_id] = (
            time.monotonic(),
            replace(
                row, requested_outfit_id=oid, request_changes=row.request_changes + 1
            ),
        )
    _prewarm_portrait(user_id, level)
    return outcome


def outfit_payload(
    o: Outfit, *, reason: str, source: str, level: int
) -> dict[str, Any]:
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
