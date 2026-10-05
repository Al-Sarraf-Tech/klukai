"""Command Decisions: she asks for his call, and his answer changes her world.

Klukai brings the Commander small decisions from her own life (who takes point,
which exhaust goes on the bike, what she wears to inspection) framed as
procedure. His answer gets a one-shot acknowledgement, then visibly lands
12-36h later as a consequence line, a remembered detail of her world
(``her:*`` facts), or tomorrow's outfit (``outfit:tomorrow``).

Content is the ``command_decisions`` section of ``config/personality.yaml``.
Effects are data, never code: ``{"outfit": id}`` or
``{"fact": {"key": "her:<slug>", "value": text}}``.

Facts written (all via ``memory.store_fact``, all fail-soft):

- ``decision:pending``: JSON ``{id, ask, options, asked_at}``, 24h TTL. At
  most one pending, expires silently, never re-asked or chased.
- ``decision:last:<id>``: JSON ``{date, choice}``. Written at ask time
  (choice null) and again at resolution. Drives the weekly cap and the
  30-day reuse rule.
- ``outfit:tomorrow``: JSON ``{"date": "YYYY-MM-DD", "outfit_id": id}``.
  ``date`` is tomorrow's Commander-local date at resolution; TTL 172800s.
- ``her:<slug>``: free text, no TTL.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any

from . import context
from .personality import load_personality
from .proactive.state import LOCAL_TZ

if TYPE_CHECKING:
    from .proactive.engine import ProactiveEngine

logger = logging.getLogger(__name__)

PRIMARY_USER = "jalsarraf"  # scheduled jobs are single-user (see engine BOUNDARY note)

TIER_MIN = {"operational": 0, "personal": 3, "matters": 6}
DECISIONS_MIN_LEVEL = 2
ASK_CHANCE = 0.4              # per 14:40 slot, before the weekly cap
MAX_ASKS_PER_WEEK = 2         # rolling 7 local days, today included
REUSE_COOLDOWN_DAYS = 30

PENDING_FACT = "decision:pending"
LAST_FACT = "decision:last:"
OUTFIT_TOMORROW_FACT = "outfit:tomorrow"
HER_PREFIX = "her:"
PENDING_TTL = 24 * 3600
LAST_TTL = 40 * 86400
OUTFIT_TOMORROW_TTL = 172800
PENDING_CLEAR_TTL = 60        # no delete API: overwrite with "" and let it lapse

CONSEQUENCE_MIN = timedelta(hours=12)
CONSEQUENCE_MAX = timedelta(hours=36)
DELIVERY_FROM = time(9, 0)    # the "message" deferred kind ignores quiet hours,
DELIVERY_UNTIL = time(21, 0)  # so the delay itself lands inside the day

HER_BLOCK_MIN_LEVEL = 3
HER_BLOCK_MAX = 4
HER_VALUE_MAX = 160
KEYWORD_MAX_LEN = 160         # keyword answers only count in short messages

_CACHE_TTL = 300.0
_HER_CACHE_TTL = 600.0
_pending_cache: dict[str, tuple[float, dict | None]] = {}
_her_cache: dict[str, tuple[float, list[dict]]] = {}

_HER_KEY = re.compile(r"her:[a-z0-9_]+")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _local_today(now: datetime) -> date:
    return now.astimezone(LOCAL_TZ).date()


# ── Templates ────────────────────────────────────────────────────────────────


def _option(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    label = str(raw.get("label") or "").strip()
    consequence = str(raw.get("consequence") or "").strip()
    keywords = raw.get("keywords")
    if not label or not consequence or not isinstance(keywords, list):
        return None
    out: dict[str, Any] = {
        "key": str(raw.get("key") or "").strip().upper(),
        "label": label,
        "keywords": [str(k).strip().lower() for k in keywords if str(k).strip()],
        "consequence": consequence,
        "disagree": bool(raw.get("disagree", False)),
    }
    if isinstance(raw.get("effect"), dict):
        out["effect"] = raw["effect"]
    return out


def load_templates(p: dict) -> list[dict]:
    """Validated, normalised templates from ``command_decisions.templates``."""
    section = p.get("command_decisions")
    raw = section.get("templates") if isinstance(section, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        options = [_option(o) for o in t.get("options") or []]
        tid = str(t.get("id") or "").strip()
        ask = str(t.get("ask") or "").strip()
        tier = t.get("tier")
        try:
            min_level = int(t.get("min_level", 0))
        except (TypeError, ValueError):
            continue
        if not tid or not ask or tier not in TIER_MIN:
            continue
        if not 2 <= len(options) <= 3 or any(o is None for o in options):
            continue
        out.append({"id": tid, "tier": tier, "min_level": min_level, "ask": ask,
                    "options": options})
    return out


def eligible_level(t: dict) -> int:
    """Lowest affection level at which a template may be asked."""
    return max(int(t["min_level"]), TIER_MIN[t["tier"]], DECISIONS_MIN_LEVEL)


def render_ask(t: dict) -> str:
    """Her line, with ``{options}`` rendered as "A: label. B: label."."""
    opts = " ".join(f"{o['key']}: {o['label']}." for o in t["options"])
    if "{options}" in t["ask"]:
        return t["ask"].replace("{options}", opts)
    return f"{t['ask']} {opts}"


# ── Answer parsing ───────────────────────────────────────────────────────────

_WORDS = re.compile(r"[a-z0-9#']+")
_FILLERS = frozenset({
    "ok", "okay", "hmm", "hm", "um", "uh", "well", "then", "please", "definitely",
    "obviously", "sure", "yeah", "yes", "i", "guess", "think", "go", "going", "with",
    "the", "option", "choice", "letter", "lets", "let's", "i'll", "ill", "i'd", "id",
    "say", "pick", "take", "choose", "answer", "my", "is",
})
_NEGATIONS = frozenset({"not", "no", "never", "don't", "dont", "nah", "without"})
_ORDINALS = {"first": 0, "second": 1, "third": 2, "last": -1, "former": 0, "latter": -1}
_DIGITS = {"1": 0, "2": 1, "3": 2}

_CUE_LETTER = re.compile(r"\b(?:option|choice|answer)\s*[:#]?\s*([abc])\b")
_VERB_LETTER = re.compile(
    r"\b(?:go(?:ing)?\s+with|pick|take|choose|chose|say|vote(?:\s+for)?|prefer)\s+([abc])\b"
    r"(?=\s*(?:$|[.!,;)]|\s+(?:please|then|for\s+sure|definitely|obviously|i\s+think)))"
)
_LEAD_LETTER = re.compile(r"^\s*([abc])\s*(?:[.!,:;)\-]|$)")
_UPPER_LETTER = re.compile(r"(?<![A-Za-z'])([BC])(?![A-Za-z'\-])")
_ORDINAL_PHRASE = re.compile(r"\b(first|second|third|last)\s+(?:one|option|choice)\b"
                             r"|\bthe\s+(former|latter)\b")
_DIGIT_CUE = re.compile(r"(?:\b(?:option|choice|number)|#)\s*([123])\b")


def _negated(text: str, start: int) -> bool:
    """A negation in the same clause, within 3 words before ``start``."""
    clause = re.split(r"[.,;!?]", text[:start])[-1]
    return any(w in _NEGATIONS for w in _WORDS.findall(clause)[-3:])


def _explicit_keys(text: str, original: str, keys: list[str]) -> set[str]:
    """Keys named by letter, ordinal or number."""
    found: list[tuple[str, int]] = []   # (key, match start)

    def by_index(i: int) -> str | None:
        return keys[i] if -len(keys) <= i < len(keys) else None

    core = [w for w in _WORDS.findall(text) if w not in _FILLERS]
    if len(core) == 1:
        w = core[0]
        if w.upper() in keys:
            found.append((w.upper(), 0))
        elif w in _ORDINALS or w in _DIGITS:
            k = by_index(_ORDINALS[w] if w in _ORDINALS else _DIGITS[w])
            if k:
                found.append((k, 0))
    for rx in (_CUE_LETTER, _VERB_LETTER, _LEAD_LETTER):
        found += [(m.group(1).upper(), m.start()) for m in rx.finditer(text)]
    found += [(m.group(1), m.start()) for m in _UPPER_LETTER.finditer(original)]
    for m in _ORDINAL_PHRASE.finditer(text):
        k = by_index(_ORDINALS[m.group(1) or m.group(2)])
        if k:
            found.append((k, m.start()))
    for m in _DIGIT_CUE.finditer(text):
        k = by_index(_DIGITS[m.group(1)])
        if k:
            found.append((k, m.start()))
    return {k for k, start in found if k in keys and not _negated(text, start)}


def parse_decision_answer(message: str, options: list[dict]) -> str | None:
    """Which option ``message`` picks: its key, or None if none/ambiguous.

    Letters ("B", "option b", "go with a"), ordinals ("the second one", "the
    latter") and numbers ("#2") win outright. Otherwise option keywords and
    labels are scored, ignoring negated hits ("not the loud one"), in
    messages up to ~160 characters. Questions never resolve.
    """
    if not isinstance(message, str) or not options:
        return None
    original = message.strip().replace("’", "'")
    text = original.lower()
    if not text or text.endswith("?"):
        return None
    keys = [str(o.get("key", "")).upper() for o in options]

    explicit = _explicit_keys(text, original, keys)
    if explicit:
        return explicit.pop() if len(explicit) == 1 else None
    if len(text) > KEYWORD_MAX_LEN:
        return None

    scores: dict[str, int] = {}
    for key, opt in zip(keys, options, strict=True):
        phrases = {*(opt.get("keywords") or []), str(opt.get("label", "")).lower()}
        hits = 0
        for phrase in filter(None, phrases):
            m = re.search(r"\b" + re.escape(phrase) + r"\b", text)
            if m and not _negated(text, m.start()):
                hits += 1
        scores[key] = hits
    best = max(scores.values())
    winners = [k for k, v in scores.items() if v == best]
    return winners[0] if best > 0 and len(winners) == 1 else None


# ── Consequence timing ───────────────────────────────────────────────────────


def pick_consequence_delay(now: datetime, roll: float) -> float:
    """Seconds until the consequence: 12-36h out, landing 09:00-21:00 local.

    ``roll`` in [0, 1] picks a point uniformly across the allowed time. Any
    24h span holds about 12h of 09:00-21:00, so a window always exists.
    """
    lo, hi = now + CONSEQUENCE_MIN, now + CONSEQUENCE_MAX
    windows: list[tuple[datetime, datetime]] = []
    day = lo.astimezone(LOCAL_TZ).date()
    while day <= hi.astimezone(LOCAL_TZ).date():
        start = max(lo, datetime.combine(day, DELIVERY_FROM, tzinfo=LOCAL_TZ))
        end = min(hi, datetime.combine(day, DELIVERY_UNTIL, tzinfo=LOCAL_TZ))
        if start < end:
            windows.append((start, end))
        day += timedelta(days=1)
    target = min(max(roll, 0.0), 1.0) * sum((e - s).total_seconds() for s, e in windows)
    for start, end in windows[:-1]:
        span = (end - start).total_seconds()
        if target <= span:
            return (start - now).total_seconds() + target
        target -= span
    start, end = windows[-1]
    return (start - now).total_seconds() + min(target, (end - start).total_seconds())


# ── Blocks ───────────────────────────────────────────────────────────────────


def build_decision_block(pending: dict, key: str) -> str:
    """One-shot block: he answered her question; acknowledge and comply."""
    opt = next(o for o in pending["options"] if o.get("key") == key)
    lines = [
        f"COMMAND DECISION: Earlier you asked him: \"{pending.get('ask', '')}\" "
        f"He chose {key}: {opt['label']}.",
        "Acknowledge his call in character, in one sentence at most, then answer "
        "the rest of what he said. Don't announce what happens next; it will "
        "happen on its own.",
    ]
    if opt.get("disagree"):
        lines.append(
            "It's not the call you would have made. Let that show for a beat, then "
            "comply without arguing: \"...Fine. You're the Commander.\""
        )
    return "\n".join(lines)


def build_her_world_block(entries: list[dict]) -> str:
    """The last HER_BLOCK_MAX non-empty ``her:*`` details, in store order."""
    lines: list[str] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        value = str(e.get("value") or "").strip()
        if not value:
            continue
        slug = str(e.get("key", "")).rsplit(HER_PREFIX, 1)[-1]
        lines.append(f"- {slug.replace('_', ' ')}: {value[:HER_VALUE_MAX]}")
    if not lines:
        return ""
    return (
        "HER WORLD (small things in your own life the Commander decided; mention "
        "one only when it fits, never list them):\n" + "\n".join(lines[-HER_BLOCK_MAX:])
    )


# ── Fact helpers (all fail-soft) ─────────────────────────────────────────────


def _asked_at(data: dict) -> datetime:
    asked = datetime.fromisoformat(str(data["asked_at"]))
    return asked if asked.tzinfo else asked.replace(tzinfo=timezone.utc)


def _parse_pending(raw: Any) -> dict | None:
    """A well-formed pending decision from the stored JSON, else None."""
    try:
        data = json.loads(raw) if raw else None
        if not isinstance(data, dict) or not data.get("id"):
            return None
        options = data.get("options")
        if not isinstance(options, list) or not options or not all(
            isinstance(o, dict) and o.get("key") and o.get("label") for o in options
        ):
            return None
        _asked_at(data)
    except (TypeError, ValueError, KeyError):
        return None
    return data


async def _load_pending(memory: Any, user_id: str, now: datetime) -> dict | None:
    """The live pending decision (cached 5 min; expiry re-checked every read)."""
    cached = _pending_cache.get(user_id)
    if cached and _time.monotonic() - cached[0] < _CACHE_TTL:
        data = cached[1]
    else:
        try:
            raw = await memory.recall_fact(PENDING_FACT, user_id=user_id)
        except Exception as e:
            logger.debug("decision:pending recall failed: %s", e)
            raw = None
        data = _parse_pending(raw)
        _pending_cache[user_id] = (_time.monotonic(), data)
    if data and now - _asked_at(data) < timedelta(seconds=PENDING_TTL):
        return data
    return None


async def _history(memory: Any, user_id: str) -> dict[str, date]:
    """template id -> local date it was last asked or answered."""
    try:
        entries = await memory.recall_facts_by_pattern(f"{LAST_FACT}%", user_id=user_id)
    except Exception as e:
        logger.debug("decision history recall failed: %s", e)
        return {}
    out: dict[str, date] = {}
    for entry in entries or []:
        if not isinstance(entry, dict) or LAST_FACT not in str(entry.get("key", "")):
            continue
        try:
            when = date.fromisoformat(json.loads(entry.get("value") or "")["date"])
        except (TypeError, ValueError, KeyError):
            continue
        out[str(entry["key"]).rsplit(LAST_FACT, 1)[-1]] = when
    return out


async def _store(memory: Any, key: str, value: str, user_id: str, ttl: int | None = None) -> None:
    kwargs: dict[str, Any] = {"user_id": user_id} if ttl is None else {"ttl": ttl, "user_id": user_id}
    try:
        await memory.store_fact(key, value, **kwargs)
    except Exception as e:
        logger.warning("decision fact %s not stored: %s", key, e)


async def apply_effect(memory: Any, user_id: str, effect: Any, now: datetime) -> None:
    """Apply a data-only effect. Anything unrecognised is ignored."""
    if not isinstance(effect, dict):
        return
    outfit = effect.get("outfit")
    fact = effect.get("fact")
    if isinstance(outfit, str) and outfit.strip():
        tomorrow = _local_today(now) + timedelta(days=1)
        await _store(memory, OUTFIT_TOMORROW_FACT,
                     json.dumps({"date": tomorrow.isoformat(), "outfit_id": outfit.strip()}),
                     user_id, ttl=OUTFIT_TOMORROW_TTL)
    elif isinstance(fact, dict) and _HER_KEY.fullmatch(str(fact.get("key", ""))):
        await _store(memory, str(fact["key"]), str(fact.get("value", "")), user_id)
        _her_cache.pop(user_id, None)
    else:
        logger.warning("Ignoring unsupported decision effect: %r", effect)


# ── Asking ───────────────────────────────────────────────────────────────────


async def maybe_ask(engine: ProactiveEngine, user_id: str) -> bool:
    """The 14:40 slot: maybe bring him a decision. True iff she asked."""
    level = engine._affection_level
    if level < DECISIONS_MIN_LEVEL or not engine._can_send():
        return False
    if random.random() > ASK_CHANCE:
        return False
    if engine._game_active_probe is not None and await engine._game_active_probe():
        return False

    memory = context.memory
    now = _utcnow()
    if await _load_pending(memory, user_id, now):
        return False
    today = _local_today(now)
    history = await _history(memory, user_id)
    if sum(1 for d in history.values() if (today - d).days < 7) >= MAX_ASKS_PER_WEEK:
        return False
    eligible = [
        t for t in load_templates(load_personality())
        if eligible_level(t) <= level
        and (today - history.get(t["id"], date.min)).days >= REUSE_COOLDOWN_DAYS
    ]
    if not eligible:
        return False

    t = random.choice(eligible)
    if not await engine._deliver(render_ask(t)):
        return False
    pending = {"id": t["id"], "ask": render_ask(t), "options": t["options"],
               "asked_at": now.isoformat()}
    _pending_cache[user_id] = (_time.monotonic(), pending)
    await _store(memory, PENDING_FACT, json.dumps(pending), user_id, ttl=PENDING_TTL)
    await _store(memory, LAST_FACT + t["id"],
                 json.dumps({"date": today.isoformat(), "choice": None}), user_id, ttl=LAST_TTL)
    logger.info("Command decision asked: %s", t["id"])
    return True


# ── Resolution + per-message blocks ─────────────────────────────────────────


async def resolve_prompt_block(user_id: str, content: str, memory: Any) -> str:
    """If ``content`` answers her pending decision: apply it and return the
    one-shot COMMAND DECISION block. Otherwise ``""``; she never nags.
    Never raises: this runs on the chat path."""
    try:
        return await _resolve(user_id, content, memory)
    except Exception as e:
        logger.warning("Command decision resolution skipped: %s", e)
        return ""


async def _resolve(user_id: str, content: str, memory: Any) -> str:
    now = _utcnow()
    pending = await _load_pending(memory, user_id, now)
    if not pending:
        return ""
    key = parse_decision_answer(content, pending["options"])
    if key is None:
        return ""
    _pending_cache[user_id] = (_time.monotonic(), None)  # resolve exactly once
    opt = next(o for o in pending["options"] if o.get("key") == key)

    await _store(memory, PENDING_FACT, "", user_id, ttl=PENDING_CLEAR_TTL)
    await _store(memory, LAST_FACT + str(pending["id"]),
                 json.dumps({"date": _local_today(now).isoformat(), "choice": key}),
                 user_id, ttl=LAST_TTL)
    await apply_effect(memory, user_id, opt.get("effect"), now)
    try:
        from . import deferred
        await deferred.schedule(
            {"kind": "message", "text": str(opt["consequence"])},
            user_id=user_id,
            delay_seconds=pick_consequence_delay(now, random.random()),
        )
    except Exception as e:
        logger.warning("Decision consequence not scheduled: %s", e)
    logger.info("Command decision %s resolved: %s", pending["id"], key)
    return build_decision_block(pending, key)


async def her_world_prompt_block(user_id: str, affection_level: int, memory: Any) -> str:
    """Per-message HER WORLD block at affection 3+ (cached for 10 minutes)."""
    if affection_level < HER_BLOCK_MIN_LEVEL:
        return ""
    cached = _her_cache.get(user_id)
    if cached and _time.monotonic() - cached[0] < _HER_CACHE_TTL:
        entries = cached[1]
    else:
        try:
            entries = list(await memory.recall_facts_by_pattern(f"{HER_PREFIX}%", user_id=user_id))
        except Exception as e:
            logger.debug("her:* recall failed: %s", e)
            entries = []
        _her_cache[user_id] = (_time.monotonic(), entries)
    return build_her_world_block(entries)
