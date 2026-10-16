"""Validated briefing configuration and authored-line presentation."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)
SLOTS = ("brief", "sendoff", "debrief")
VARIANTS = ("normal", "medical", "sensitive")
DEFAULT_MIN_AFFECTION = 2
DEFAULT_SLIP_FROM = 5

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


class _Slots(dict[str, str]):
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
