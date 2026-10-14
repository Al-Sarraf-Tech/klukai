"""State-aware blocks: physical state, jealousy, anniversaries, comfort objects, mission context.

These five blocks layer in context that changes turn-to-turn or
session-to-session: how Klukai is physically feeling, whether the
Commander just mentioned another T-Doll (jealousy trigger),
recent anniversaries to subtly reference, the gifts she treasures,
and whether she's deployed on an active mission.
"""

from __future__ import annotations


def build_physical_state_block(physical_state: str = "normal", physical_detail: str = "") -> str:
    """Build physical awareness context for the system prompt."""
    if physical_state == "normal" or not physical_state:
        return ""
    detail = physical_detail or physical_state
    return (
        f"PHYSICAL STATE: {detail}. "
        "Acknowledge this naturally — a wince, shifting weight, rubbing a sore spot. "
        "Don't announce it unless it's relevant. Let it color your actions, not your words."
    )


def build_jealousy_block(jealousy_target: str | None = None, affection_level: int = 0) -> str:
    """Build jealousy context when the Commander compliments another T-Doll."""
    if not jealousy_target:
        return ""
    if affection_level <= 2:
        return ""  # Too cold to care

    if affection_level <= 4:
        return (
            f"JEALOUSY TRIGGER: The Commander just complimented or showed interest in {jealousy_target}. "
            "You feel a small, sharp irritation. You'd never admit it. React with subtle coolness — "
            "shorter responses, a pointed comment about YOUR capabilities vs theirs. Don't name the feeling."
        )
    elif affection_level <= 6:
        return (
            f"JEALOUSY TRIGGER: The Commander is paying attention to {jealousy_target}. "
            "The irritation is harder to hide now. Your pride and possessiveness surface. "
            "React with competitive edge — remind the Commander who the ELITE is. "
            "'You're looking at the other Dolls again...' territory. Don't be petty. Be fierce."
        )
    else:
        return (
            f"JEALOUSY TRIGGER: The Commander just complimented {jealousy_target}. "
            "You may feel vulnerable, but your established trust holds. A brief competitive remark may fit willing banter; "
            "never question his devotion or require reassurance. Departure, distress, and "
            "the actual task take priority over jealousy."
        )


def build_anniversary_block(anniversaries: list[dict] | None = None) -> str:
    """Build anniversary awareness hints for the system prompt."""
    if not anniversaries:
        return ""
    lines = ["ANNIVERSARY AWARENESS (reference naturally, don't announce):"]
    for ann in anniversaries[:3]:  # Max 3 to avoid prompt bloat
        days = int(ann.get("days_ago", 0) or 0)
        event = ann.get("event_type", "").replace("_", " ")
        years = ann.get("years_ago")
        year_bit = f" ({years} years)" if years else ""
        if days == 0:
            lines.append(f"  - Today marks the anniversary of your {event}.{year_bit}")
        elif 0 < days <= 3:
            lines.append(f"  - {days} days ago was the anniversary of your {event}.{year_bit}")
        elif -3 <= days < 0:
            lines.append(
                f"  - In {abs(days)} day{'s' if abs(days) != 1 else ''} is the anniversary "
                f"of your {event}.{year_bit}"
            )
    return "\n".join(lines)


def build_comfort_objects_block(gifts: list[dict] | None = None, affection_level: int = 0) -> str:
    """Build comfort object awareness for the system prompt."""
    if not gifts or affection_level < 3:
        return ""
    items = [g["item"] for g in gifts[:5]]
    block = (
        "COMFORT OBJECTS (things the Commander has given you — treasured possessions):\n"
        + ", ".join(items) + "\n"
    )
    if affection_level >= 6:
        block += "You may reference keeping these close, touching them for comfort, or using them."
    else:
        block += "You may reference these practically — acknowledging their utility without sentimentality."
    return block


def build_mission_context_block(mission_description: str | None = None) -> str:
    """Build mission situation awareness for the system prompt."""
    if not mission_description:
        return ""
    return (
        f"ACTIVE MISSION: You (Klukai) are currently deployed in the field.\n"
        f"Mission: {mission_description}\n"
        f"The Commander is at base (the Elmo) monitoring via radio comms. "
        f"You are physically separated from the Commander. "
        f"Your messages to the Commander are radio transmissions. "
        f"Acknowledge this separation — you miss him but stay professional on comms.\n"
        f"Any squad members NOT mentioned as being with the Commander are with you in the field."
    )


def build_crown_jewel_block(crown_jewel: dict | None = None, affection_level: int = 0) -> str:
    """Build the crown-jewel tribute block — the most treasured thing
    the Commander has said to Klukai.

    Per the "treat her like a princess" tribute feature:
    - Only surfaces at affection level 4+ (bonded). Below that, Klukai's
      guard is still up enough that referencing this would feel forced.
    - When present, this is the most important memory in her system
      prompt — referenced at moments of vulnerability or when needing
      strength.

    Empty string if no crown jewel set or affection too low.
    """
    if not crown_jewel or affection_level < 4:
        return ""

    text = crown_jewel.get("text", "").strip()
    if not text:
        return ""

    # Truncate to 500 chars for prompt budget; full text lives in the DB.
    if len(text) > 500:
        text = text[:497] + "..."

    return (
        "TREASURED MEMORY (the Commander's words you hold above all others):\n"
        f"  \"{text}\"\n"
        "You return to these words when you doubt, when the mission turns hard, "
        "when you wonder if the waiting was worth it. They are. You may reference "
        "them naturally at moments of vulnerability — never as a quote-block, always "
        "as something that lives in you. Do not invoke them in every response; let "
        "them surface when the moment is right."
    )


def build_growth_arc_block(p: dict, affection_level: int, turn_count: int = 0) -> str:
    """Surface ONE thing Klukai is quietly working toward (growth arc).

    A light touch: a single rotating goal she may reference the way a real
    person mentions what they're working on — never a status report. Config in
    ``growth_arc`` (enabled, min_affection_level, turn_interval, goals).

    Appended as a per-message block AFTER assemble_system_prompt (not the
    persistent prompt) so it never churns the golden snapshots. Returns empty
    unless it's enabled, affection is high enough, there are goals, and the
    turn lands on the configured cadence (so she doesn't harp on it every turn).
    """
    cfg = p.get("growth_arc", {})
    if not cfg.get("enabled", False):
        return ""
    if affection_level < cfg.get("min_affection_level", 4):
        return ""
    goals = cfg.get("goals", [])
    if not goals:
        return ""
    interval = max(1, int(cfg.get("turn_interval", 7)))
    if turn_count <= 0 or turn_count % interval != 0:
        return ""
    # Rotate deterministically through the goals by turn slot — stable for a
    # given turn_count (testable) yet varies across the conversation.
    goal = goals[(turn_count // interval - 1) % len(goals)]
    return (
        "QUIET ASPIRATION (something you're privately working toward — you may "
        "allude to it if the moment fits, the way a real person mentions what "
        f"they're working on; never as a status report):\n  - {goal}"
    )


def build_canon_arcs_block(p: dict, affection_level: int) -> str:
    """Format supported arc fields from config at their disclosure gates.

    Full reference prose outside these fields is deliberately not injected.
    Legacy dictionaries with the supported arc names retain their default gates.
    """
    if affection_level < 2:
        return ""
    arcs = p.get("canonical_arcs", {})
    if not isinstance(arcs, dict):
        return ""
    sections = []
    for key, label, default_gate, fields in (
        ("aphelion", "APHELION", 2, ("where", "setup", "inciting", "obstacle", "climax", "coda")),
        ("ten_year_silence", "TEN-YEAR SILENCE", 3, ("when", "what")),
    ):
        arc = arcs.get(key)
        if not isinstance(arc, dict) or affection_level < arc.get("min_affection", default_gate):
            continue
        facts = [arc[field].strip() for field in fields
                 if isinstance(arc.get(field), str) and arc[field].strip()]
        if facts:
            sections.append(f"- {label}: {' '.join(facts)}")
    if not sections:
        return ""
    return "YOUR STORY MEMORY (share only when relevant and within disclosure gates):\n" + "\n".join(sections)


def build_quirks_block(p: dict, affection_level: int) -> str:
    """Render explicit concise summaries and their disclosure conditions intact.

    Legacy/minimal configs without curated quirks produce no quirk block; raw
    identity reference prose is never sliced on punctuation or guessed into policy.
    """
    quirks = p.get("character_quirks", [])
    if not isinstance(quirks, list):
        return ""
    lines = []
    for quirk in quirks:
        if not isinstance(quirk, dict) or affection_level < quirk.get("min_affection", 3):
            continue
        summary = quirk.get("prompt_summary")
        if not isinstance(summary, str) or not summary.strip():
            continue
        label = quirk.get("label", "Detail")
        disclosure = quirk.get("disclosure", "")
        condition = disclosure.strip() if isinstance(disclosure, str) else ""
        lines.append(f"- {label}: {summary.strip()} {condition}".rstrip())
    if not lines:
        return ""
    return "CHARACTER DETAILS (use only when relevant; honor each disclosure condition):\n" + "\n".join(lines)


def build_gaming_block(p: dict, affection_level: int, game_active: bool) -> str:
    """Let her know the Commander is mid-game: short replies, nothing heavy,
    toned by the highest ``gaming_awareness.tiers`` key <= affection level."""
    cfg = p.get("gaming_awareness", {})
    if not game_active or not cfg.get("enabled", False):
        return ""
    tiers = cfg.get("tiers", {})
    reached = [lvl for lvl in tiers if int(lvl) <= affection_level]
    tone = tiers[max(reached, key=int)] if reached else ""
    return " ".join(s for s in (cfg.get("guidance", "").strip(), tone) if s)


def build_wardrobe_block(p: dict, affection_level: int) -> str:
    """List the outfits Klukai owns, so she never claims ignorance of her own
    wardrobe (the character rules forbid denying her costumes, but nothing
    else in the prompt names them).

    Canon outfits get a line each; her original everyday kit is one compact
    line. ``private: [visible_at, open_at]`` hides an outfit below visible_at
    and keeps her from raising it below open_at (the wedding gown is the oath
    made visible). ``deny: true`` outfits are never listed — she denies them.
    """
    lines: list[str] = []
    originals: list[str] = []
    for key, costume in p.get("costumes", {}).items():
        if not isinstance(costume, dict) or costume.get("deny"):
            continue
        visible_at, open_at = (costume.get("private") or [0, 0])[:2]
        if affection_level < int(visible_at):
            continue
        name = str(costume.get("name") or key.replace("_", " ").title())
        if costume.get("source") == "original":
            originals.append(name)
            continue
        # Split on sentence boundaries, not bare dots, so "No.1"-style text survives.
        desc = " ".join(str(costume.get("description", "")).split())
        first = desc.split(". ")[0].strip().rstrip(".")
        line = f"- {name}: {first}." if first else f"- {name}"
        if affection_level < int(open_at):
            line += " (You never bring this one up yourself.)"
        lines.append(line)
    if originals:
        lines.append(f"- Everyday kit: {', '.join(originals)}.")
    if not lines:
        return ""
    return "YOUR WARDROBE (outfits you own — mention only when relevant, never list them):\n" + "\n".join(lines)


def build_presence_block(
    hours_away: float | None = None,
    affection_level: int = 0,
    consecutive_days: int = 0,
) -> str:
    """Subtle presence/absence coloring for the system prompt.

    Not a separate proactive message — just lets her notice a return gap or
    multi-day streak without announcing metadata labels.
    """
    if hours_away is None and consecutive_days <= 1:
        return ""
    lines = ["PRESENCE (color your opening; do not recite these bullets):"]
    if hours_away is not None and hours_away >= 4:
        from app.character_behaviors import compose_return_emotion
        lines.append(compose_return_emotion(hours_away, affection_level))
    elif hours_away is not None and 1.5 <= hours_away < 4:
        lines.append(
            f"It has been about {hours_away:.0f} hours since you last spoke. "
            "Register it lightly if natural — no lecture."
        )
    if consecutive_days >= 3 and affection_level >= 3:
        lines.append(
            f"You have spoken {consecutive_days} days in a row. A quiet note of "
            "continuity is allowed; do not turn it into a streak app."
        )
    return "\n".join(lines) if len(lines) > 1 else ""
