"""Operation Brief — the pure planning layer.

Everything here is deterministic and touches nothing outside its arguments:
no DB, no clock read, no engine. ``op_brief`` owns the rail-facing side
(storage, cancellation, ``on_turn``, delivery) and imports from here.

- classification: ``classify`` (variant), ``category`` (de-dup name),
  ``normalize_what`` (display form of the model's noun phrase);
- when-hint resolution: ``resolve_when`` turns "thursday at 9", "Oct 12",
  "in 3 days" into a ``When`` inside the planning window — or None;
- the chain: ``plan_slots`` (brief / sendoff / debrief as naive local
  datetimes, past slots removed), ``accept_event`` (extractor row → storable
  commitment), ``when_of`` (commitment → ``When``);
- ``hold_deadline``: the latest a held (mid-game / goodbye-hold) slot may
  still go out;
- ``mentions``: does a message refer to this event (for cancellation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

KIND = "event"

MIN_CONFIDENCE = 0.75
WINDOW_DAYS = 21

BRIEF_AT = time(20, 30)
SENDOFF_AT = time(8, 5)
DEBRIEF_AT = time(19, 0)
NEXT_DAY_DEBRIEF_AT = time(10, 0)
LATEST_EVENING = time(22, 45)
SENDOFF_LEAD = timedelta(minutes=15)
DEBRIEF_AFTER = timedelta(minutes=90)
HOLD_MAX = timedelta(hours=3)

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


def hold_deadline(action: dict[str, Any], commitment: dict[str, Any]) -> datetime:
    """Latest moment a held (mid-game) slot may still go out."""
    due = datetime.fromisoformat(str(action["due"]))
    deadline = min(due + HOLD_MAX, datetime.combine(due.date(), LATEST_EVENING))
    when = when_of(commitment)
    if action.get("slot") == "sendoff" and when.at is not None:
        deadline = min(deadline, datetime.combine(when.day, when.at) - SENDOFF_LEAD)
    return deadline


# ── cancellation matching ───────────────────────────────────────────────────


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
