"""Operation Brief — she prepares him for his real-life events.

When the Commander mentions something he has to face ("interview Thursday",
"dentist tomorrow at 3"), the promise extractor's single LLM call also returns
it as an *event*. An accepted event becomes at most one chain of three deferred
one-shots on the ``deferred`` rail (action kind ``op_brief``):

- ``brief``   — T-1 at 20:30 local: a short OPORD in her voice.
- ``sendoff`` — T-0 at 08:05 local: one line. (08:05 rather than 07:55: quiet
  hours end at 08:00 and every slot honours them.)
- ``debrief`` — T-0 at 19:00 local, later if the event runs late: "Report."

Slots already in the past are skipped. Acts of service, not nagging: one chain
per event, and every slot goes through the proactive guard (mute, quiet hours,
daily cap). A slot that lands mid-game is held and retried until a deadline.

Variants:

- ``sensitive`` (funeral, hospital, surgery, court, breakup…) — a quiet
  presence line instead of an OPORD. No humour, no "Report." Distress is met
  with protection, never irritation.
- ``medical`` (dentist, doctor, scans…) — briefed, but never told what to eat,
  drink or take. She defers to *their* instructions.
- ``normal`` — everything else.

Storage: rows in ``companion_promises`` with ``commitment.kind = "event"`` and
``scheduled_followup`` NULL, so the promise follow-up path never treats them as
his promises (``promises.due_promises`` also excludes the kind explicitly).
Additive only. Two stamps are ever written after insert: ``followup_sent_at``
when the chain completes, and ``resolved_at`` + ``sentiment='cancelled'`` when
he says it's off.

Copy is authored in ``config/personality.yaml`` (``operation_brief:``) and read
at fire time, so edits hot-reload. Built-in defaults cover a missing or broken
section. Planning is fail-soft; delivery lets *pre-send* failures propagate so
the deferred rail retries them, and swallows everything after the line is out
so a retry can never double-send.
"""

from __future__ import annotations

import json
import logging
import random
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from . import deferred
from .db import get_conn
from .proactive.state import LOCAL_TZ, now_local

logger = logging.getLogger(__name__)

KIND = "event"
ACTION_KIND = "op_brief"
SLOTS = ("brief", "sendoff", "debrief")
VARIANTS = ("normal", "medical", "sensitive")

MIN_CONFIDENCE = 0.75
WINDOW_DAYS = 21
MAX_EVENTS_PER_TURN = 3

BRIEF_AT = time(20, 30)
SENDOFF_AT = time(8, 5)
DEBRIEF_AT = time(19, 0)
NEXT_DAY_DEBRIEF_AT = time(10, 0)
LATEST_EVENING = time(22, 45)
SENDOFF_LEAD = timedelta(minutes=15)
DEBRIEF_AFTER = timedelta(minutes=90)
HOLD_STEP_SECONDS = 900
HOLD_MAX = timedelta(hours=3)

DEFAULT_MIN_AFFECTION = 2
DEFAULT_SLIP_FROM = 5

# ── classification ──────────────────────────────────────────────────────────

_SENSITIVE_RE = re.compile(
    r"\b(funeral|memorial|burial|wake|hospital|hospice|surgery|chemo\w*|biopsy|"
    r"oncolog\w*|icu|court|hearing|trial|lawyer|custody|divorce|break-?up|"
    r"sentencing|deposition|eviction)\b",
    re.I,
)
_MEDICAL_RE = re.compile(
    r"\b(doctor|dr|dentist|dental|checkup|check-up|physical|clinic|blood ?(?:test|work|draw)|"
    r"lab ?work|scan|mri|x-?ray|ultrasound|vaccin\w*|therap\w*|physio\w*|"
    r"optometrist|eye exam|dermatolog\w*|procedure|medical)\b",
    re.I,
)

# Ordered: the first match names the event for de-duplication, so two
# mentions of "the interview" on the same day collapse into one chain.
_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rf"\b(?:{pattern})\b", re.I))
    for name, pattern in (
        ("funeral", r"funeral|memorial|burial|wake"),
        ("surgery", r"surgery|procedure|biopsy"),
        ("hospital", r"hospital|hospice"),
        ("court", r"court|hearing|trial|deposition|sentencing"),
        ("flight", r"flight|flying|plane"),
        ("interview", r"interviews?"),
        ("presentation", r"presentation|pitch|demo|talk|speech"),
        ("medical", r"doctor|dr|dentist|dental|checkup|check-up|physical|clinic|scan|mri|x-?ray|therap\w*"),
        ("exam", r"exams?|test|midterms?|finals?|quiz"),
    )
)

_CATEGORY_MAP = dict(_CATEGORIES)

_STOPWORDS = frozenset(
    "a an the my your his her our their at with for to on in of and or "
    "this that next tomorrow today tonight".split()
)


def classify(what: str, llm_sensitivity: str | None = None) -> str:
    """``sensitive`` > ``medical`` > ``normal``.

    The model may only *raise* sensitivity: a keyword hit always wins, and an
    LLM "sensitive" is trusted even without one. Nothing can talk a funeral
    down to a cheerful OPORD.
    """
    if _SENSITIVE_RE.search(what) or (llm_sensitivity or "").strip().lower() == "sensitive":
        return "sensitive"
    if _MEDICAL_RE.search(what):
        return "medical"
    return "normal"


def category(what: str) -> str:
    for name, pattern in _CATEGORIES:
        if pattern.search(what):
            return name
    words = [w for w in re.findall(r"[a-z0-9']+", what.lower()) if w not in _STOPWORDS]
    return " ".join(words) or "event"


def normalize_what(what: str) -> str:
    """Clean the model's noun phrase for display: "my X"/"his X" become
    "your X" (she is talking *to* him); bare articles are dropped."""
    text = " ".join(str(what).split()).strip(" .,!?;:")[:80].strip()
    lowered = text.lower()
    for prefix in ("my ", "his "):
        if lowered.startswith(prefix):
            return "your " + text[len(prefix):]
    for prefix in ("a ", "an ", "the "):
        if lowered.startswith(prefix):
            return text[len(prefix):]
    return text


# ── when-hint resolution ────────────────────────────────────────────────────

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_NUMBER_WORDS = {
    "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_ALT = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))

_TIME_COLON_RE = re.compile(r"\b(\d{1,2}):([0-5]\d)\s*(a\.?m\.?|p\.?m\.?)?")
_TIME_SUFFIX_RE = re.compile(r"\b(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?![a-z])")
_TIME_MILITARY_RE = re.compile(r"\b([01]\d|2[0-3])([0-5]\d)\s*(?:hours|hrs|h)\b")
_TIME_AT_RE = re.compile(r"\bat\s+(\d{1,2})\b(?!\s*(?:st|nd|rd|th|/|-))")
_ISO_RE = re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b")
_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_MONTH_DAY_RE = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b")
_DAY_MONTH_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_ALT})\b")
_ORDINAL_RE = re.compile(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b")
_IN_DAYS_RE = re.compile(r"\bin\s+(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")\s+days?\b")
_IN_WEEK_RE = re.compile(r"\bin\s+(?:a|one)\s+week\b|\ba\s+week\s+from\s+(?:today|now)\b")
_WEEKDAY_RE = re.compile(rf"\b(?:(next|this|on)\s+)?({_WEEKDAY_ALT})\b")

# Approximate hours for part-of-day words: used to time the debrief, never shown.
_PART_OF_DAY = (("morning", time(9, 0)), ("afternoon", time(14, 0)),
                ("evening", time(19, 30)), ("tonight", time(19, 30)))


@dataclass(frozen=True)
class When:
    day: date
    at: time | None
    exact: bool


def _hour_from(hour: int, suffix: str | None) -> int | None:
    if suffix:
        if not 1 <= hour <= 12:
            return None
        pm = suffix.startswith("p")
        return (hour % 12) + (12 if pm else 0)
    if hour > 23:
        return None
    if 1 <= hour <= 7:
        return hour + 12  # "at 3" means the afternoon, not 0300
    return hour


def _extract_time(text: str) -> tuple[time | None, str]:
    """Return (time, text-with-the-time-removed) so "3" in "at 3pm" can't be
    mistaken for a day of the month afterwards."""
    if re.search(r"\bnoon\b", text):
        return time(12, 0), re.sub(r"\bnoon\b", " ", text)
    m = _TIME_MILITARY_RE.search(text)
    if m:
        return time(int(m.group(1)), int(m.group(2))), text[:m.start()] + " " + text[m.end():]
    m = _TIME_COLON_RE.search(text)
    if m:
        hour = _hour_from(int(m.group(1)), m.group(3))
        if hour is not None:
            return time(hour, int(m.group(2))), text[:m.start()] + " " + text[m.end():]
    m = _TIME_SUFFIX_RE.search(text)
    if m:
        hour = _hour_from(int(m.group(1)), m.group(2))
        if hour is not None:
            return time(hour, 0), text[:m.start()] + " " + text[m.end():]
    m = _TIME_AT_RE.search(text)
    if m:
        hour = _hour_from(int(m.group(1)), None)
        if hour is not None:
            return time(hour, 0), text[:m.start()] + " " + text[m.end():]
    return None, text


def _next_month_day(today: date, day: int) -> date:
    candidate = date(today.year, today.month, day)
    if candidate >= today:
        return candidate
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(year, month, day)


def _month_day(today: date, month: int, day: int) -> date:
    candidate = date(today.year, month, day)
    return candidate if candidate >= today else date(today.year + 1, month, day)


def _weekday(today: date, prefix: str | None, weekday: int) -> date:
    ahead = (weekday - today.weekday()) % 7
    if prefix == "next":
        target = today + timedelta(days=ahead or 7)
        # "next Friday" = the Friday of next calendar week (weeks start Monday).
        if target.isocalendar()[:2] == today.isocalendar()[:2]:
            target += timedelta(days=7)
        return target
    if ahead == 0 and prefix != "this":
        return today + timedelta(days=7)  # "on Monday", said on a Monday
    return today + timedelta(days=ahead)


def _extract_day(text: str, today: date) -> date | None:
    if "day after tomorrow" in text:
        return today + timedelta(days=2)
    if re.search(r"\b(tomorrow|tmrw|tmr)\b", text):
        return today + timedelta(days=1)
    if re.search(r"\b(today|tonight|this (?:morning|afternoon|evening))\b", text):
        return today
    m = _IN_DAYS_RE.search(text)
    if m:
        raw = m.group(1)
        return today + timedelta(days=int(raw) if raw.isdigit() else _NUMBER_WORDS[raw])
    if _IN_WEEK_RE.search(text):
        return today + timedelta(days=7)
    m = _ISO_RE.search(text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _MONTH_DAY_RE.search(text)
    if m:
        return _month_day(today, _MONTHS[m.group(1)], int(m.group(2)))
    m = _DAY_MONTH_RE.search(text)
    if m:
        return _month_day(today, _MONTHS[m.group(2)], int(m.group(1)))
    m = _SLASH_RE.search(text)
    if m:
        if m.group(3):
            year = int(m.group(3))
            return date(year + 2000 if year < 100 else year, int(m.group(1)), int(m.group(2)))
        return _month_day(today, int(m.group(1)), int(m.group(2)))
    m = _ORDINAL_RE.search(text)
    if m:
        return _next_month_day(today, int(m.group(1)))
    m = _WEEKDAY_RE.search(text)
    if m:
        return _weekday(today, m.group(1), _WEEKDAYS[m.group(2)])
    return None


def resolve_when(hint: Any, now: datetime) -> When | None:
    """Resolve the Commander's words for *when* into a local day (+ time).

    Accepts only something concrete inside the next ``WINDOW_DAYS`` days:
    "tomorrow at 9", "Thursday", "Oct 12", "in 3 days". "Next week",
    "soon" and dates in the past resolve to None — no guessing.
    """
    if not isinstance(hint, str) or not hint.strip():
        return None
    text = " " + hint.lower().strip() + " "
    today = now.date()
    at, rest = _extract_time(text)
    exact = at is not None
    try:
        day = _extract_day(rest, today)
    except (ValueError, KeyError):
        return None  # "Feb 30", "13/45"
    if day is None:
        if at is None:
            return None
        day = today  # "at 3pm" with no day means today
    if at is None:
        for word, approx in _PART_OF_DAY:
            if word in rest:
                at = approx
                break
    if not 0 <= (day - today).days <= WINDOW_DAYS:
        return None
    if day == today and exact and at is not None and at <= now.time():
        return None  # already happened
    return When(day=day, at=at, exact=exact)


# ── scheduling ──────────────────────────────────────────────────────────────


def plan_slots(when: When, now: datetime) -> list[tuple[str, datetime]]:
    """The chain for one event as naive local datetimes, past slots removed."""
    event_dt = datetime.combine(when.day, when.at) if when.at else None
    slots: list[tuple[str, datetime]] = [
        ("brief", datetime.combine(when.day - timedelta(days=1), BRIEF_AT)),
    ]
    sendoff = datetime.combine(when.day, SENDOFF_AT)
    if event_dt is None or sendoff <= event_dt - SENDOFF_LEAD:
        slots.append(("sendoff", sendoff))
    debrief = datetime.combine(when.day, DEBRIEF_AT)
    if event_dt is not None:
        debrief = max(debrief, event_dt + DEBRIEF_AFTER)
    if debrief.date() > when.day or debrief.time() > LATEST_EVENING:
        debrief = datetime.combine(when.day + timedelta(days=1), NEXT_DAY_DEBRIEF_AT)
    slots.append(("debrief", debrief))
    return [(slot, due) for slot, due in slots if due > now]


def accept_event(raw: Any, now: datetime) -> dict[str, Any] | None:
    """Validate one extractor event and turn it into a storable commitment."""
    if not isinstance(raw, dict):
        return None
    what = raw.get("what")
    if not isinstance(what, str) or not what.strip():
        return None
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    if confidence < MIN_CONFIDENCE:
        return None
    when = resolve_when(raw.get("when_hint"), now)
    if when is None:
        return None
    clean = normalize_what(what)
    return {
        "kind": KIND,
        "what": clean,
        "when_hint": raw.get("when_hint"),
        "event_date": when.day.isoformat(),
        "event_time": when.at.strftime("%H:%M") if when.at else None,
        "time_exact": when.exact,
        "variant": classify(clean, raw.get("sensitivity")),
        "dedupe_key": f"{when.day.isoformat()}|{category(clean)}",
        "confidence": confidence,
    }


def when_of(commitment: dict[str, Any]) -> When:
    at_raw = commitment.get("event_time")
    at = datetime.strptime(at_raw, "%H:%M").time() if at_raw else None
    return When(
        day=date.fromisoformat(commitment["event_date"]),
        at=at,
        exact=bool(commitment.get("time_exact")),
    )


# ── storage (companion_promises, kind=event) ────────────────────────────────


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


async def store_event(commitment: dict[str, Any], user_id: str) -> str | None:
    """Insert one event row unless the same open event already exists.

    Returns the new row id, or None for a duplicate or a DB failure.
    ``scheduled_followup`` stays NULL: event rows are never promise follow-ups.
    """
    try:
        async with get_conn() as conn:
            dup = await (await conn.execute(
                "SELECT id FROM companion_promises "
                "WHERE user_id = %s AND resolved_at IS NULL "
                "AND commitment->>'kind' = %s AND commitment->>'dedupe_key' = %s "
                "LIMIT 1",
                (user_id, KIND, commitment["dedupe_key"]),
            )).fetchone()
            if dup:
                return None
            row = await (await conn.execute(
                "INSERT INTO companion_promises "
                "(user_id, promise_text, commitment, scheduled_followup) "
                "VALUES (%s, %s, %s, NULL) RETURNING id",
                (user_id, commitment["what"], json.dumps(commitment)),
            )).fetchone()
            await conn.commit()
        return str(row[0]) if row else None
    except Exception as e:
        logger.warning("store_event failed for %s: %s", user_id, e)
        return None


async def open_events(user_id: str) -> list[dict[str, Any]]:
    """Unresolved, unfinished event rows (newest first). Fail-soft to []."""
    try:
        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT id, promise_text, commitment FROM companion_promises "
                "WHERE user_id = %s AND resolved_at IS NULL AND followup_sent_at IS NULL "
                "AND commitment->>'kind' = %s ORDER BY made_at DESC LIMIT 20",
                (user_id, KIND),
            )).fetchall()
        return [{"id": str(r[0]), "what": r[1], "commitment": _as_dict(r[2])} for r in rows]
    except Exception as e:
        logger.warning("open_events failed for %s: %s", user_id, e)
        return []


async def load_event(event_id: str, user_id: str) -> dict[str, Any] | None:
    """The live event for a firing slot, or None if it is gone, cancelled,
    finished or not an event. DB errors propagate so the rail retries."""
    try:
        uuid.UUID(event_id)
    except ValueError:
        return None
    async with get_conn() as conn:
        row = await (await conn.execute(
            "SELECT promise_text, commitment, resolved_at, followup_sent_at "
            "FROM companion_promises WHERE id = %s AND user_id = %s",
            (event_id, user_id),
        )).fetchone()
    if not row or row[2] is not None or row[3] is not None:
        return None
    commitment = _as_dict(row[1])
    if commitment.get("kind") != KIND or not commitment.get("event_date"):
        return None
    return commitment


async def _stamp(sql: str, params: tuple[Any, ...]) -> None:
    try:
        async with get_conn() as conn:
            await conn.execute(sql, params)
            await conn.commit()
    except Exception as e:
        logger.warning("op_brief stamp failed: %s", e)


async def mark_complete(event_id: str, user_id: str) -> None:
    await _stamp(
        "UPDATE companion_promises SET followup_sent_at = NOW() "
        "WHERE id = %s AND user_id = %s AND followup_sent_at IS NULL",
        (event_id, user_id),
    )


async def mark_cancelled(event_id: str, user_id: str, note: str) -> None:
    await _stamp(
        "UPDATE companion_promises SET resolved_at = NOW(), sentiment = 'cancelled', "
        "response_text = %s WHERE id = %s AND user_id = %s AND resolved_at IS NULL",
        (note[:200], event_id, user_id),
    )


# ── cancellation ────────────────────────────────────────────────────────────

_CANCEL_RE = re.compile(
    r"\b(cancel\w*|call(?:ed)? off|postpon\w*|reschedul\w*|moved (?:it )?to|pushed back)\b", re.I
)
_NEGATED_CANCEL_RE = re.compile(
    r"\b(?:not|never|no longer)\s+(?:been\s+|be\s+|getting\s+)?(?:cancel|call|postpon|reschedul)"
    r"|n't\s+(?:been\s+|be\s+|getting\s+)?(?:cancel|call|postpon|reschedul)",
    re.I,
)


def mentions(message: str, commitment: dict[str, Any]) -> bool:
    """Does ``message`` refer to this event (by category or a content word)?"""
    lowered = message.lower()
    pattern = _CATEGORY_MAP.get(category(str(commitment.get("what") or "")))
    if pattern is not None and pattern.search(lowered):
        return True
    words = re.findall(r"[a-z0-9']+", str(commitment.get("what") or "").lower())
    return any(
        len(w) >= 4 and w not in _STOPWORDS and re.search(rf"\b{re.escape(w)}\b", lowered)
        for w in words
    )


async def cancel_mentioned(message: str, user_id: str) -> int:
    """Close any open event he says is cancelled or moved. Regex first, so a
    normal turn never touches the DB. Returns how many were cancelled."""
    if not _CANCEL_RE.search(message) or _NEGATED_CANCEL_RE.search(message):
        return 0
    cancelled = 0
    for event in await open_events(user_id):
        if mentions(message, event["commitment"]):
            await mark_cancelled(event["id"], user_id, message)
            cancelled += 1
    if cancelled:
        logger.info("Operation brief: %d event(s) cancelled for %s", cancelled, user_id)
    return cancelled


# ── planning entry point (background extraction) ────────────────────────────


async def on_turn(
    user_msg: str,
    events: Any,
    *,
    user_id: str,
    affection_level: int,
) -> int:
    """Cancel what he called off, then plan chains for new events.

    Runs in background extraction after the reply. Cancellation goes first so
    "moved to Friday" retires Thursday's chain before Friday's is planned.
    Returns the number of chains scheduled. Never raises.
    """
    try:
        await cancel_mentioned(user_msg, user_id)
        if not isinstance(events, list) or not events:
            return 0
        if affection_level < load_config().min_affection:
            return 0
        now = now_local()
        planned = 0
        for raw in events[:MAX_EVENTS_PER_TURN]:
            commitment = accept_event(raw, now)
            if commitment is None:
                continue
            slots = plan_slots(when_of(commitment), now)
            if not slots:
                continue
            event_id = await store_event(commitment, user_id)
            if not event_id:
                continue
            for slot, due in slots:
                task_id = await deferred.schedule(
                    {"kind": ACTION_KIND, "event_id": event_id, "slot": slot,
                     "due": due.isoformat()},
                    user_id=user_id,
                    due_at=due.replace(tzinfo=LOCAL_TZ),
                )
                if task_id is None:
                    logger.warning("Operation brief %s for %s could not be scheduled",
                                   slot, event_id)
            planned += 1
            logger.info("Operation brief planned for %s: %s on %s (%s)",
                        user_id, commitment["what"][:40], commitment["event_date"],
                        ",".join(s for s, _ in slots))
        return planned
    except Exception as e:
        logger.warning("Operation brief planning failed for %s: %s", user_id, e)
        return 0


# ── copy ────────────────────────────────────────────────────────────────────

_DEFAULT_POOLS: dict[str, dict[str, list[str]]] = {
    "normal": {
        "brief": [
            "Briefing for tomorrow.\nSITUATION: {your_what}, tomorrow{when}.\n"
            "MISSION: Go in, get it done.\nEXECUTION: Prepare tonight, not at dawn.\n"
            "SUSTAINMENT: Eat breakfast. That's not a suggestion."
        ],
        "sendoff": ["Today{when}: {the_what}. You're prepared. Go."],
        "debrief": ["Report."],
    },
    "medical": {
        "brief": [
            "Noted for tomorrow{when}: {your_what}. Follow their instructions to the "
            "letter. Theirs, not mine. Leave early."
        ],
        "sendoff": ["Today{when}: {your_what}. Their instructions, exactly. Go."],
        "debrief": ["How did {the_what} go? Keep it short if you like."],
    },
    "sensitive": {
        "brief": ["I know what tomorrow is. No briefing for this one. ...I'm here."],
        "sendoff": ["...I'm here today, Commander. That's all."],
        "debrief": ["...I'm here. No report needed."],
    },
}
_DEFAULT_SLIP = ["...Come back and tell me. That's not a request either."]


@dataclass(frozen=True)
class BriefConfig:
    min_affection: int
    slip_from: int
    slip_lines: list[str]
    pools: dict[str, dict[str, dict[int, list[str]]]]


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _lines(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v.strip()]


def _pool(value: Any) -> dict[int, list[str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[int, list[str]] = {}
    for key, lines in value.items():
        level = _int(key, -1)
        clean = _lines(lines)
        if level >= 0 and clean:
            out[level] = clean
    return out


def load_config() -> BriefConfig:
    """Read ``operation_brief:`` at call time (hot reload), with defaults for
    anything missing or malformed."""
    try:
        from .personality import load_personality
        section = load_personality().get("operation_brief")
    except Exception as e:
        logger.warning("operation_brief config unavailable, using defaults: %s", e)
        section = None
    if not isinstance(section, dict):
        section = {}
    pools: dict[str, dict[str, dict[int, list[str]]]] = {}
    for variant in VARIANTS:
        authored = section.get(variant)
        authored = authored if isinstance(authored, dict) else {}
        pools[variant] = {
            slot: _pool(authored.get(slot)) or {0: _DEFAULT_POOLS[variant][slot]}
            for slot in SLOTS
        }
    return BriefConfig(
        min_affection=_int(section.get("min_affection"), DEFAULT_MIN_AFFECTION),
        slip_from=_int(section.get("slip_from_affection"), DEFAULT_SLIP_FROM),
        slip_lines=_lines(section.get("slip_lines")) or _DEFAULT_SLIP,
        pools=pools,
    )


class _Slots(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _slot_values(commitment: dict[str, Any]) -> _Slots:
    what = str(commitment.get("what") or "it")
    possessive = what.lower().startswith("your ")
    when = ""
    if commitment.get("time_exact") and commitment.get("event_time"):
        when = " at " + str(commitment["event_time"]).replace(":", "")
    return _Slots(
        what=what,
        the_what=what if possessive else f"the {what}",
        your_what=what if possessive else f"your {what}",
        when=when,
    )


def _fill(template: str, values: _Slots, fallback: str) -> str:
    try:
        return template.format_map(values)
    except (ValueError, IndexError, AttributeError):
        return fallback.format_map(values)


def render(slot: str, commitment: dict[str, Any], level: int, cfg: BriefConfig) -> str:
    """Pick and fill the line for ``slot`` at this affection level."""
    variant = commitment.get("variant")
    if variant not in VARIANTS:
        variant = "normal"
    pool = cfg.pools[variant][slot]
    band = max((k for k in pool if k <= level), default=min(pool))
    values = _slot_values(commitment)
    text = _fill(random.choice(pool[band]), values, _DEFAULT_POOLS[variant][slot][0])
    if slot == "brief" and variant != "sensitive" and level >= cfg.slip_from:
        text = f"{text}\n{random.choice(cfg.slip_lines)}"
    text = text.strip()
    return text[:1].upper() + text[1:]


# ── delivery (deferred rail → dispatch → here) ──────────────────────────────


def hold_deadline(action: dict[str, Any], commitment: dict[str, Any]) -> datetime:
    """Latest moment a held (mid-game) slot may still go out."""
    due = datetime.fromisoformat(str(action["due"]))
    deadline = min(due + HOLD_MAX, datetime.combine(due.date(), LATEST_EVENING))
    when = when_of(commitment)
    if action.get("slot") == "sendoff" and when.at is not None:
        deadline = min(deadline, datetime.combine(when.day, when.at) - SENDOFF_LEAD)
    return deadline


async def _game_active(router: Any) -> bool:
    try:
        return bool(await router.is_game_active())
    except Exception:
        return False


async def deliver(user_id: str, action: dict[str, Any]) -> None:
    """Fire one slot. Held while a game is running (until its deadline);
    dropped when the proactive guard says no (muted, quiet hours, daily cap)
    or when affection has fallen below the gate. The debrief closes the chain
    whether it was sent or dropped."""
    slot = str(action.get("slot") or "")
    event_id = str(action.get("event_id") or "")
    if slot not in SLOTS or not event_id or not action.get("due"):
        logger.warning("Malformed op_brief action for %s: %r", user_id, action)
        return
    commitment = await load_event(event_id, user_id)
    if commitment is None:
        return  # cancelled, already finished, or gone

    from .context import affection, proactive, router, ws

    cfg = load_config()
    level = (await affection.get_state(user_id)).level
    if level < cfg.min_affection:
        await _close(slot, event_id, user_id)
        return
    if await _game_active(router):
        if now_local() < hold_deadline(action, commitment):
            await deferred.schedule(dict(action), user_id=user_id,
                                    delay_seconds=HOLD_STEP_SECONDS)
            return
        logger.info("Operation brief %s dropped: game ran past the hold window", slot)
        await _close(slot, event_id, user_id)
        return
    if not proactive._can_send(ignore_unanswered=True):
        logger.info("Operation brief %s suppressed by the proactive guard", slot)
        await _close(slot, event_id, user_id)
        return

    text = render(slot, commitment, level, cfg)
    await ws.send_proactive(user_id, text)  # persists to history as model=proactive
    try:
        if not ws.is_connected(user_id):
            from .push import send_push
            await send_push(title="Klukai", body=text, user_id=user_id)
        # Count it like any proactive line so the daily cap and the
        # don't-pile-up gate see it.
        proactive._proactive_count_today += 1
        proactive._last_proactive_answered = False
    except Exception as e:
        logger.warning("Operation brief post-send bookkeeping failed: %s", e)
    await _close(slot, event_id, user_id)
    logger.info("Operation brief %s delivered to %s", slot, user_id)


async def _close(slot: str, event_id: str, user_id: str) -> None:
    if slot == "debrief":
        await mark_complete(event_id, user_id)
