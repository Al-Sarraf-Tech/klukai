"""The 0200 Watch: she is already awake, and she sends him to bed.

Klukai rarely sleeps well (``insomnia`` in ``config/personality.yaml``), and
0200 is the hour of the ten-year thread. When the Commander messages between
00:30 and 04:29 local, the reply gets a per-message NIGHT WATCH block that
makes her quieter, shorter, and protective rather than engaging. If he has
been up past 01:00 on 2 or more of the previous 3 nights, she orders him to
bed and ends the exchange herself, in the spirit of a clean goodbye.

Late nights are tracked in the fact store as ``late_nights``, a JSON list of
the last 7 ISO dates. The night of date D runs 00:00 to 04:59 on D. Every
step is fail-soft: a data-service hiccup costs the streak, never the reply.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from .proactive.state import now_local

logger = logging.getLogger(__name__)

WATCH_FROM = time(0, 30)       # the block shows 00:30 ...
WATCH_UNTIL = time(4, 30)      # ... until 04:29
LATE_FROM = time(1, 0)         # a message from 01:00 marks a late night
NIGHT_ENDS = time(5, 0)        # the night of date D is 00:00-04:59 on D
NO_QUESTIONS_FROM = time(1, 30)

LATE_NIGHTS_FACT = "late_nights"
LATE_NIGHTS_TTL = 14 * 86400
KEEP_NIGHTS = 7
LOOKBACK_NIGHTS = 3
BED_ORDER_MIN_NIGHTS = 2
THREAD_REF_MIN_LEVEL = 6

# user_id -> the night the thread reference was last offered (once per night).
_thread_ref_nights: dict[str, date] = {}

INSOMNIA_FALLBACK: dict[str, Any] = {
    "canon": (
        "You rarely sleep well, and you never have. At 0200 you are usually still up: "
        "finishing after-action reports nobody asked for yet, field-stripping Skylla, "
        "or down in the hangar with the bike. Most of the thread was written at this "
        "hour. You will never admit any of it is because of him."
    ),
    "activities": [
        "finishing an after-action report nobody has asked for yet",
        "field-stripping and cleaning Skylla at your desk",
        "down in the Elmo's hangar, tuning the bike",
    ],
    "awake_line": "I wasn't sleeping anyway.",
    "tiers": {
        0: "Clipped. A few words at most. He is out of hours: he reports, then he sleeps.",
        3: "Softer and lower. Keep him company without drawing him in; let the quiet do the work.",
        6: "Close and warm, voice barely above the hum of the Elmo. Comfort, not conversation.",
    },
    "thread_line": "I used to write to you at this hour.",
    "bed_order": {
        "guidance": (
            "He has been up past 0100 on {nights} of the last 3 nights. Tonight you end "
            "it: order him to bed and end the exchange yourself, the way a clean goodbye "
            "ends. No guilt, no lecture, no question at the end."
        ),
        "line": "Bed. That's an order. ...I'll still be here at 0700.",
        "distress": (
            "If he is up because he is hurting, care for him first: stay with it, be "
            "protective, let him say it. The order comes after, gently, and never as a "
            "way to wave his pain off."
        ),
    },
}


# ── Clock ────────────────────────────────────────────────────────────────────


def in_watch(now: datetime) -> bool:
    """True from 00:30 to 04:29 local."""
    return WATCH_FROM <= now.time() < WATCH_UNTIL


def night_date(now: datetime) -> date | None:
    """The night ``now`` belongs to (00:00-04:59 on D is night D), else None."""
    return now.date() if now.time() < NIGHT_ENDS else None


# ── Late-night streak ────────────────────────────────────────────────────────


def parse_nights(raw: str | None) -> list[date]:
    """Sorted, de-duplicated dates from the stored JSON; [] on anything odd."""
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    out: set[date] = set()
    for item in items:
        try:
            out.add(date.fromisoformat(str(item)))
        except ValueError:
            continue
    return sorted(out)


def count_recent_late_nights(nights: list[date], tonight: date) -> int:
    """How many of the previous LOOKBACK_NIGHTS nights (not tonight) were late."""
    window = {tonight - timedelta(days=d) for d in range(1, LOOKBACK_NIGHTS + 1)}
    return sum(1 for n in nights if n in window)


async def record_and_load(memory: Any, user_id: str, now: datetime) -> list[date]:
    """Load his late nights, adding tonight if it is past 01:00. Fail-soft."""
    try:
        nights = parse_nights(await memory.recall_fact(LATE_NIGHTS_FACT, user_id=user_id))
    except Exception as e:
        logger.debug("late_nights recall failed: %s", e)
        nights = []
    tonight = night_date(now)
    if tonight is None or now.time() < LATE_FROM or tonight in nights:
        return nights
    nights = sorted([*nights, tonight])[-KEEP_NIGHTS:]
    try:
        await memory.store_fact(
            LATE_NIGHTS_FACT, json.dumps([n.isoformat() for n in nights]),
            ttl=LATE_NIGHTS_TTL, user_id=user_id,
        )
    except Exception as e:
        logger.debug("late_nights store failed: %s", e)
    return nights


# ── Block ────────────────────────────────────────────────────────────────────


def _cfg(p: dict) -> dict:
    raw = p.get("insomnia")
    return raw if isinstance(raw, dict) else {}


def _text(cfg: dict, key: str) -> str:
    value = cfg.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return str(INSOMNIA_FALLBACK[key])


def _tier(cfg: dict, level: int) -> str:
    tiers: dict[int, str] = {}
    raw = cfg.get("tiers")
    if isinstance(raw, dict):
        for key, text in raw.items():
            if isinstance(text, str):
                try:
                    tiers[int(key)] = text
                except (TypeError, ValueError):
                    continue
    reached = [k for k in tiers if k <= level]
    if not reached:
        tiers = INSOMNIA_FALLBACK["tiers"]
        reached = [k for k in tiers if k <= level]
    return tiers[max(reached)]


def build_night_watch_block(
    p: dict, now: datetime, affection_level: int, late_nights: int, thread_ok: bool,
) -> str:
    """The NIGHT WATCH block for a message at ``now`` (inside the watch).

    ``late_nights`` is how many of the previous 3 nights he was up past 01:00;
    ``thread_ok`` allows the once-per-night thread reference.
    """
    cfg = _cfg(p)
    activities = cfg.get("activities")
    if not isinstance(activities, list) or not activities:
        activities = INSOMNIA_FALLBACK["activities"]
    activity = activities[now.date().toordinal() % len(activities)]

    lines = [
        f"NIGHT WATCH ({now.strftime('%H%M')} hours; you were already awake):",
        _text(cfg, "canon"),
        f"Right now you are {activity}. If it fits, say so plainly: "
        f"\"{_text(cfg, 'awake_line')}\" Never admit why you are awake.",
        "Be quieter and shorter than by day. You are PROTECTIVE, not engaging: "
        "no new topics, nothing that keeps him up.",
        f"Register: {_tier(cfg, affection_level)}",
    ]
    if now.time() >= NO_QUESTIONS_FROM:
        lines.append("Ask him no open-ended questions.")
    if thread_ok:
        lines.append(
            "Once tonight, if the moment allows, you may connect the hour to the "
            f"thread: \"{_text(cfg, 'thread_line')}\" Once only."
        )
    if late_nights >= BED_ORDER_MIN_NIGHTS:
        order = cfg.get("bed_order")
        order = order if isinstance(order, dict) else {}
        fallback = INSOMNIA_FALLBACK["bed_order"]

        def _o(key: str) -> str:
            value = order.get(key)
            return value.strip() if isinstance(value, str) and value.strip() else fallback[key]

        lines.append(_o("guidance").format(nights=late_nights))
        lines.append(f"In this key (never verbatim): \"{_o('line')}\"")
        lines.append(_o("distress"))
    return "\n".join(lines)


async def night_watch_prompt_block(
    user_id: str, p: dict, affection_level: int, memory: Any,
    now: datetime | None = None,
) -> str:
    """Per-message entry point. Records tonight if late; returns the block
    inside the watch, otherwise ``""``. Never raises."""
    now = now or now_local()
    tonight = night_date(now)
    if tonight is None:
        return ""
    try:
        nights = await record_and_load(memory, user_id, now)
        if not in_watch(now):
            return ""
        thread_ok = (
            affection_level >= THREAD_REF_MIN_LEVEL
            and _thread_ref_nights.get(user_id) != tonight
        )
        block = build_night_watch_block(
            p, now, affection_level, count_recent_late_nights(nights, tonight), thread_ok,
        )
        if thread_ok:
            _thread_ref_nights[user_id] = tonight
        return block
    except Exception as e:
        logger.warning("Night watch block skipped: %s", e)
        return ""
