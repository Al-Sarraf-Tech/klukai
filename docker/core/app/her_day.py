"""Her Day — the duty roster Klukai lives by, and where she is right now.

The roster is DERIVED, never stored: one activity per slot, picked
deterministically from (day, user, the weather snapshot the day was chosen
under), so it is stable all day and different tomorrow. Config lives in
``her_day:`` in config/personality.yaml.

``her_now`` composes the roster with the wardrobe into the one answer every
surface shares — the system prompt, the PWA status line, and image generation
all agree on where she is and what she has on.

The status colours her first line at most. Nothing here ever makes her
unavailable: she answers him at once, whatever the roster says.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from datetime import date

from . import wardrobe
from .personality.loader import load_personality
from .proactive.state import now_local

# Picks the Commander made for a special day survive the roster: nobody
# changes into coveralls on Halloween or out of the wedding gown.
_ROSTER_PROOF = frozenset({"seasonal", "oath", "formal"})
_OFF_DUTY = ("Off duty", "")


@dataclass(frozen=True)
class Block:
    slot: str
    start: int
    end: int
    id: str
    location: str
    activity: str
    duty: bool = False
    private: bool = False
    outfit: str | None = None

    def label(self, level: int) -> tuple[str, str]:
        """What the PWA may show. Below affection 3 her off-duty life is private."""
        if self.private and level < 3:
            return _OFF_DUTY
        return self.location, self.activity


def _rng(seed: str, day: date) -> random.Random:
    digest = hashlib.sha256(f"roster:{seed}:{day.isoformat()}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def build_day(cfg: dict, day: date, level: int, *, weather: dict | None = None, seed: str = "") -> list[Block]:
    """The day's roster, one block per slot, ordered by start hour."""
    rng = _rng(seed, day)
    condition = (weather or {}).get("condition")
    activities = [a for a in cfg.get("activities") or () if isinstance(a, dict)]
    blocks: list[Block] = []
    for slot, (start, end) in sorted((cfg.get("slots") or {}).items(), key=lambda kv: kv[1][0]):
        candidates = [
            a for a in activities
            if slot in (a.get("slots") or ())
            and level >= int(a.get("min_level", 0))
            and (not a.get("weekdays") or day.weekday() in a["weekdays"])
            and condition not in (a.get("avoid_conditions") or ())
        ]
        if not candidates:
            continue
        a = rng.choices(candidates, weights=[float(c.get("weight", 1.0)) for c in candidates])[0]
        blocks.append(Block(
            slot=slot, start=int(start), end=int(end), id=str(a["id"]),
            location=str(a.get("location", "The Elmo")), activity=str(a.get("activity", "")),
            duty=bool(a.get("duty", False)), private=bool(a.get("private", False)),
            outfit=a.get("outfit"),
        ))
    return blocks


def current_block(blocks: list[Block], hour: int) -> Block | None:
    return next((b for b in blocks if b.start <= hour < b.end), None)


def effective_outfit(
    row: wardrobe.HerDayRow, block: Block | None, level: int, cat: dict[str, wardrobe.Outfit],
) -> tuple[str, str]:
    """(outfit_id, source): his granted request > special-day pick > block kit > her pick."""
    requested = wardrobe.canonical_id(row.requested_outfit_id, cat)
    if requested and wardrobe.is_unlocked(requested, level, cat):
        return requested, "commander"
    base = wardrobe.canonical_id(row.base_outfit_id, cat)
    if base and not wardrobe.is_unlocked(base, level, cat):
        base = None
    special = base is not None and cat[base].category in _ROSTER_PROOF
    if not special and block and block.outfit and wardrobe.is_unlocked(block.outfit, level, cat):
        return str(block.outfit), "roster"
    if base:
        return base, "auto"
    return wardrobe.DEFAULT_OUTFIT, "auto"


@dataclass(frozen=True)
class HerNow:
    day: date
    hour: int
    outfit: wardrobe.Outfit
    outfit_source: str
    outfit_reason: str
    location: str
    activity: str
    block: Block | None
    blocks: list[Block]
    override: str | None = None

    def outfit_line(self, level: int) -> str:
        return wardrobe.outfit_line(self.outfit, self.outfit_reason, self.outfit_source, self.hour, level)

    def location_line(self) -> str:
        where = f"{self.location} — {self.activity}" if self.activity else self.location
        return (
            f"{where}. It may colour your first line (a detail, a sound, a smudge of "
            "grease); you still answer him at once — you are never too busy for him."
        )

    def status(self, level: int) -> dict:
        if self.override or self.block is None:
            location, activity = self.location, self.activity
        else:
            location, activity = self.block.label(level)
        return {
            "location": location,
            "activity": activity,
            "label": f"{location} · {activity}" if activity else location,
            "slot": self.block.slot if self.block else None,
            "override": self.override,
        }

    def schedule(self, level: int) -> list[dict]:
        out = []
        for b in self.blocks:
            location, activity = b.label(level)
            out.append({
                "slot": b.slot,
                "start": f"{b.start:02d}00",
                "end": f"{b.end % 24:02d}00",
                "location": location,
                "activity": activity,
                "current": b is self.block,
            })
        return out


def active_mission() -> str | None:
    """The running mission's description, if she is deployed."""
    from . import context

    pro = context.proactive
    if not getattr(pro, "mission_active", False):
        return None
    timer = getattr(pro, "_mission_timer", None)
    return getattr(timer, "mission_description", None) or "on mission"


async def her_now(
    user_id: str,
    level: int,
    *,
    mood: str | None = None,
    game_active: bool = False,
    mission: str | None = None,
) -> HerNow:
    """Where she is and what she's wearing, right now. Fail-soft throughout."""
    p = load_personality()
    cat = wardrobe.catalog(p)
    row = await wardrobe.ensure_today(user_id, level, mood=mood)
    hour = now_local().hour
    blocks = build_day(p.get("her_day") or {}, row.day, level, weather=row.weather, seed=user_id)
    block = current_block(blocks, hour)
    overrides = (p.get("her_day") or {}).get("overrides") or {}

    override = "mission" if mission else "gaming" if game_active else None
    if override == "mission":
        cfg = overrides.get("mission") or {}
        location = str(cfg.get("location", "Deployed"))
        activity = mission or str(cfg.get("activity", ""))
        oid, source = wardrobe.DEFAULT_OUTFIT, "roster"
    else:
        oid, source = effective_outfit(row, block, level, cat)
        if override == "gaming":
            cfg = overrides.get("gaming") or {}
            location, activity = str(cfg.get("location", "Rec room")), str(cfg.get("activity", ""))
        elif block:
            location, activity = block.location, block.activity
        else:
            location, activity = "The Elmo", ""
    outfit = wardrobe.lookup(oid, cat)
    return HerNow(
        day=row.day, hour=hour, outfit=outfit, outfit_source=source,
        outfit_reason=row.base_reason, location=location, activity=activity,
        block=block, blocks=blocks, override=override,
    )


async def render_costume(user_id: str, level: int) -> str | None:
    """The outfit id an image of her should show right now (None = keyword fallback).

    Replaces the old per-caller ``recall_fact("costume")`` + unlock check: what
    she is actually wearing — today's pick, his granted request, or block kit.
    """
    try:
        now = await her_now(user_id, level)
    except Exception:
        return None
    return now.outfit.id if now.outfit.image_tags else None


_DRINK = re.compile(
    r"\b(?:tea|coffee|latte|espresso|cocoa|drink|something to drink|thirsty|a cup|racing calm)\b"
)


def tea_time_block(now: HerNow, message: str, level: int, p: dict | None = None) -> str:
    """Per-message block when he orders a drink while she works the counter."""
    if now.override or not now.block or now.block.id != "tea_counter":
        return ""
    lower = message.lower()
    if not _DRINK.search(lower):
        return ""
    cfg = (load_personality() if p is None else p).get("tea_time") or {}
    special = str(cfg.get("special", "Racing Calm"))
    lines = [
        f"TEA TIME: {' '.join(str(cfg.get('guidance', '')).split())}",
        f"Your special: {special} — {' '.join(str(cfg.get('special_canon', '')).split())}",
    ]
    if special.lower() in lower and level >= 5:
        lines.append(" ".join(str(cfg.get("heartfelt", "")).split()))
    return "\n".join(line for line in lines if line.strip())
