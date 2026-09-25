"""Klukai's recurring rituals: the Commander's birthday and the monthly fee settlement.

Both are delivered the first time he connects in their period, guarded by
``companion_period_deliveries`` so a reconnect or a second device can never
repeat them. Content lives in ``config/personality.yaml``
(``commander_birthday``, ``fee_settlement``).
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import date, datetime, timedelta

from . import context
from .personality import load_personality
from .proactive.state import LOCAL_TZ, now_local

logger = logging.getLogger(__name__)

BIRTHDAY_FACT = "commander_birthday"
SETTLEMENT_DELAY_SECONDS = 20  # let any return greeting land first
_SCENE_PACING_SECONDS = 3
_BIRTHDAY_CACHE_TTL = 300.0
_birthday_cache: dict[str, tuple[float, str | None]] = {}

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
]
_MONTHS = {name: i for i, name in enumerate(_MONTH_NAMES, 1)}
_MONTHS.update({name[:3]: i for i, name in enumerate(_MONTH_NAMES, 1)})
_MONTHS["sept"] = 9
_MONTH = "(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"

# "my birthday is …" / "I was born on …" — but not "my birthday party is …"
_SUBJECT = re.compile(
    r"\b(?:my\s+(?:birthday|bday|b-day)(?!\s*(?:party|present|gift|cake|dinner|plans|wish))"
    r"|i\s+was\s+born)(?P<tail>[^!?\n]{0,40})"
)
_ISO = re.compile(r"\b\d{4}-(\d{2})-(\d{2})\b")
_US_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b")  # month/day, US order
_DAY_MONTH = re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MONTH}\b")
_MONTH_DAY = re.compile(rf"\b{_MONTH}\s+{_DAY}\b")
# "it's my birthday (today|tomorrow)" / "tomorrow is my birthday" — standing
# alone, so "it's my birthday party" doesn't count.
_RELATIVE = re.compile(
    r"\b(?:(today|tomorrow)\s+is\s+my\s+(?:birthday|bday)"
    r"|it'?s\s+my\s+(?:birthday|bday)(?:\s+(today|tomorrow))?)(?!\s*[a-z])"
)


def _mmdd(month: int, day: int) -> str | None:
    try:
        date(2000, month, day)  # leap year, so Feb 29 is a valid birthday
    except ValueError:
        return None
    return f"{month:02d}-{day:02d}"


def parse_birthday(message: str, today: date) -> str | None:
    """The Commander's birthday as ``MM-DD`` if this message states it."""
    text = message.lower().replace("’", "'")
    relative = _RELATIVE.search(text)
    if relative:
        when = relative.group(1) or relative.group(2) or "today"
        d = today + timedelta(days=1 if when == "tomorrow" else 0)
        return _mmdd(d.month, d.day)
    subject = _SUBJECT.search(text)
    if not subject:
        return None
    tail = subject.group("tail")
    if m := _ISO.search(tail) or _US_NUMERIC.search(tail):
        return _mmdd(int(m.group(1)), int(m.group(2)))
    if m := _DAY_MONTH.search(tail):
        return _mmdd(_MONTHS[m.group(2)], int(m.group(1)))
    if m := _MONTH_DAY.search(tail):
        return _mmdd(_MONTHS[m.group(1)], int(m.group(2)))
    return None


def days_until_birthday(mmdd: str | None, today: date) -> int | None:
    """Days from ``today`` to his next birthday (0 = today). A Feb 29 birthday
    is kept on Feb 28 in non-leap years."""
    try:
        month, day = (int(x) for x in (mmdd or "").split("-"))
    except ValueError:
        return None
    if _mmdd(month, day) is None:
        return None
    for year in (today.year, today.year + 1):
        try:
            nxt = date(year, month, day)
        except ValueError:
            nxt = date(year, 2, 28)
        if nxt >= today:
            return (nxt - today).days
    return None  # pragma: no cover - next year's date is always >= today


def _first_birthday_together(first_interaction: datetime | None, today: date) -> bool:
    if first_interaction is None:
        return True
    return (today - first_interaction.date()).days < 365


def build_birthday_block(
    p: dict, days_until: int | None, first_together: bool, just_told: bool = False,
) -> str:
    """Per-message birthday awareness: the day itself, the moment he tells her,
    and a quiet planning window beforehand."""
    if days_until == 0:
        seasonal = p.get("identity", {}).get("adjutant_lines", {}).get("seasonal", {})
        canon = seasonal.get("birthday_yr1" if first_together else "birthday_yr2", "")
        first = "It is the first one you have shared with him. " if first_together else ""
        line = f' Your canon line for today: "{canon}" Use it in your own time, never as a recital.' if canon else ""
        return (
            f"COMMANDER'S BIRTHDAY: Today is the Commander's birthday. {first}You did not "
            f"forget and you will not let him think you did.{line} Everything else today is secondary."
        )
    if just_told:
        return (
            "The Commander just told you his birthday. File it: you will never forget it. "
            "Acknowledge it briefly and in character; don't make a fuss about filing it."
        )
    planning = p.get("commander_birthday", {}).get("planning_days", 3)
    if days_until is not None and 0 < days_until <= planning:
        plural = "s" if days_until != 1 else ""
        return (
            f"The Commander's birthday is in {days_until} day{plural}. You have already "
            "started planning. Do not mention it unless he does."
        )
    return ""


async def get_birthday(user_id: str) -> str | None:
    cached = _birthday_cache.get(user_id)
    if cached and time.monotonic() - cached[0] < _BIRTHDAY_CACHE_TTL:
        return cached[1]
    value = await context.memory.recall_fact(f"rel:{BIRTHDAY_FACT}", user_id=user_id)
    _birthday_cache[user_id] = (time.monotonic(), value)
    return value


async def remember_birthday(user_id: str, message: str) -> str | None:
    """Store his birthday if this message states it; returns ``MM-DD`` or None."""
    mmdd = parse_birthday(message, now_local().date())
    if mmdd:
        await context.memory.set_relationship_fact(BIRTHDAY_FACT, mmdd, user_id=user_id)
        _birthday_cache[user_id] = (time.monotonic(), mmdd)
    return mmdd


async def birthday_prompt_block(
    user_id: str, p: dict, first_interaction: datetime | None, message: str,
) -> str:
    just_told = await remember_birthday(user_id, message)
    today = now_local().date()
    days = days_until_birthday(just_told or await get_birthday(user_id), today)
    return build_birthday_block(
        p, days, _first_birthday_together(first_interaction, today), just_told=bool(just_told),
    )


async def claim_period(user_id: str, ritual: str, period: str) -> bool:
    """Atomically claim ``ritual`` for ``period``; False if already delivered."""
    from .db import get_conn_autocommit

    try:
        async with get_conn_autocommit() as conn:
            result = await conn.execute(
                "INSERT INTO companion_period_deliveries (user_id, ritual, period) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, ritual, period),
            )
            return bool(result.rowcount)
    except Exception as e:
        logger.warning("claim_period(%s, %s) failed: %s", ritual, period, e)
        return False


async def _deliver_scene(user_id: str, lines: list[str]) -> None:
    for line in lines:
        await context.ws.send_proactive(user_id, line)
        await asyncio.sleep(_SCENE_PACING_SECONDS)


async def maybe_deliver_birthday(user_id: str) -> bool:
    """On his birthday, greet him first — once per year."""
    today = now_local().date()
    if days_until_birthday(await get_birthday(user_id), today) != 0:
        return False
    cfg = load_personality().get("commander_birthday", {})
    first_interaction = (await context.affection.get_state(user_id)).first_interaction
    first = _first_birthday_together(first_interaction, today)
    lines = cfg.get("first_year" if first else "later_years", [])
    if not lines or not await claim_period(user_id, "birthday", str(today.year)):
        return False
    await _deliver_scene(user_id, lines)
    logger.info("Birthday greeting delivered for %s", user_id)
    return True


async def _days_checked_in(user_id: str, start: date, end: date) -> int:
    """Distinct local days in [start, end) on which the Commander messaged her."""
    from .db import get_conn

    lo = datetime(start.year, start.month, start.day, tzinfo=LOCAL_TZ)
    hi = datetime(end.year, end.month, end.day, tzinfo=LOCAL_TZ)
    try:
        async with get_conn() as conn:
            row = await (await conn.execute(
                "SELECT COUNT(DISTINCT (created_at AT TIME ZONE %s)::date) "
                "FROM companion_messages WHERE user_id = %s AND role = 'user' "
                "AND created_at >= %s AND created_at < %s",
                (str(LOCAL_TZ), user_id, lo, hi),
            )).fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        logger.warning("Settlement ledger query failed: %s", e)
        return 0


async def maybe_deliver_fee_settlement(user_id: str, delay: float = 0) -> bool:
    """The first time he connects each month, she presents her invoice for the
    month just ended, ``delay`` seconds after claiming it. Never on his
    birthday — it waits for the next connect."""
    cfg = load_personality().get("fee_settlement", {})
    invoices = {int(k): v for k, v in cfg.get("invoices", {}).items()}
    level = (await context.affection.get_state(user_id)).level
    if level < cfg.get("min_affection", 3):
        return False
    tier = max((k for k in invoices if k <= level), default=None)
    if tier is None or not invoices[tier]:
        return False
    today = now_local().date()
    if days_until_birthday(await get_birthday(user_id), today) == 0:
        return False
    if not await claim_period(user_id, "fee_settlement", today.strftime("%Y-%m")):
        return False
    await asyncio.sleep(delay)

    this_month = today.replace(day=1)
    settled = (this_month - timedelta(days=1)).replace(day=1)
    fields = {
        "month": settled.strftime("%B"),
        "days": await _days_checked_in(user_id, settled, this_month),
    }
    lines = [line.format(**fields) for line in random.choice(invoices[tier])]
    openers = cfg.get("late_openers", [])
    if today.day > cfg.get("late_after_day", 5) and openers:
        lines.insert(0, random.choice(openers).format(**fields))
    await _deliver_scene(user_id, lines)
    logger.info("Fee settlement for %s delivered to %s", fields["month"], user_id)
    return True


async def on_connect(user_id: str) -> None:
    """Connect hook: birthday first; otherwise, after a beat, the settlement."""
    try:
        if not await maybe_deliver_birthday(user_id):
            await maybe_deliver_fee_settlement(user_id, delay=SETTLEMENT_DELAY_SECONDS)
    except Exception as e:
        logger.debug("Rituals-on-connect skipped: %s", e)
