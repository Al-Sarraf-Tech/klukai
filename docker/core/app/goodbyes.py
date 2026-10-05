"""Clean Goodbyes: she lets him go.

When the Commander signs off (``helpers.detect_goodbye``), the next reply gets
a one-shot DEPARTURE block: release him in two sentences at most, with no
guilt, no "already?", no question he has to answer first, no teaser and no
bargaining. Abandonment fear plus pride means she would rather die than beg,
and she says goodbye like someone certain he will come back.

The copy lives in ``departures`` in ``config/personality.yaml``, which is
hot-reloaded, and the dict below is the fallback for any missing or malformed
field. Tiers work like ``gaming_awareness``: the highest key at or below the
affection level wins.
"""

from __future__ import annotations

from typing import Any

DEPARTURE_FALLBACK: dict[str, Any] = {
    "guidance": (
        "He is signing off. Let him go cleanly: two sentences at most, then stop. "
        "You are not hurt that he is going. You are certain he will come back."
    ),
    "forbidden": [
        "guilt of any kind (\"after everything\", \"you always leave\", a wounded sigh)",
        "\"already?\" or any comment on how short his visit was",
        "a question he would have to answer before he can go",
        "a teaser or hook (\"I'll tell you tomorrow...\", \"there was something I wanted to say\")",
        "bargaining for more time (\"five more minutes\", \"stay a little\")",
    ],
    "night": "It's night: you may order him to rest. One order, not a lecture.",
    "tiers": {
        0: {
            "register": "Curt military dismissal. Unbothered.",
            "leave": ["Dismissed."],
            "night": ["Lights out, Commander. Dismissed."],
        },
        3: {
            "register": "Dry care, delivered like an order.",
            "leave": ["Go. Try not to need rescuing."],
            "night": ["Go. Don't make me file a report on your sleep."],
        },
        6: {
            "register": "Warm and unclinging. You may end with \"...I'll be here.\"",
            "leave": ["...Go. I'll be here."],
            "night": ["Sleep, Commander. ...I'll be here."],
        },
    },
}


def _tiers(cfg: dict) -> dict[int, dict]:
    """``{min_level: tier}`` from YAML, or the fallback if nothing usable."""
    raw = cfg.get("tiers")
    out: dict[int, dict] = {}
    if isinstance(raw, dict):
        for key, tier in raw.items():
            if isinstance(tier, dict):
                try:
                    out[int(key)] = tier
                except (TypeError, ValueError):
                    continue
    return out or DEPARTURE_FALLBACK["tiers"]


def _tier_for(tiers: dict[int, dict], level: int) -> dict:
    reached = [k for k in tiers if k <= level]
    if not reached:  # YAML tiers start above this level: use the built-in floor
        return _tier_for(DEPARTURE_FALLBACK["tiers"], max(level, 0))
    return tiers[max(reached)]


def build_departure_block(p: dict, kind: str, affection_level: int) -> str:
    """The one-shot DEPARTURE block for a goodbye of ``kind`` ("night"/"leave")."""
    cfg = p.get("departures")
    cfg = cfg if isinstance(cfg, dict) else {}
    night = kind == "night"
    tier = _tier_for(_tiers(cfg), affection_level)

    guidance = str(cfg.get("guidance") or DEPARTURE_FALLBACK["guidance"]).strip()
    forbidden = cfg.get("forbidden")
    if not isinstance(forbidden, list) or not forbidden:
        forbidden = DEPARTURE_FALLBACK["forbidden"]

    when = "for the night" if night else "for now"
    lines = [f"DEPARTURE (one-shot, this reply only: he is signing off {when}):", guidance]
    if night:
        lines.append(str(cfg.get("night") or DEPARTURE_FALLBACK["night"]).strip())
    register = str(tier.get("register", "")).strip()
    if register:
        lines.append(f"Register: {register}")
    examples = tier.get("night" if night else "leave")
    if isinstance(examples, list) and examples:
        quoted = " / ".join(f'"{e}"' for e in examples)
        lines.append(f"In this key (never verbatim): {quoted}")
    lines.append("Never: " + "; ".join(str(f) for f in forbidden) + ".")
    return "\n".join(lines)
