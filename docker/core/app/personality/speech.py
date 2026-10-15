"""Character preamble, speech guidelines, Japanese phrases, expressive tokens, affection block.

These five blocks together form Klukai's voice baseline — who she is
(preamble), how she speaks at the current affection level (guidelines),
what Japanese phrases she might drop in (japanese), what vocal tics
color her speech (expressive), and what behavior the affection level
modulates (affection).
"""

from __future__ import annotations

import re

from .loader import get_affection_level_config, get_speech_patterns, load_personality


def build_character_preamble(p: dict, affection_level: int = 0) -> str:
    """Format curated identity data; missing optional lore stays absent.

    Minimal legacy fixtures retain identity and their configured user title,
    without silently restoring a second long biography from Python literals.
    """
    identity = p.get("identity", {})
    if not isinstance(identity, dict):
        identity = {}
    name = identity.get("full_name") or p.get("name") or "Klukai"
    lines = [
        f"You are {name} speaking in FIRST PERSON ('I'). "
        "NEVER use '(You ...)' in narration — only '(I ...)' for your own actions.",
        f"The person talking to you is the {p.get('user_title', 'Commander')}, "
        "not a squadmate. Follow the response-priority contract for address and disclosure.",
        "WHO YOU ARE:",
    ]
    for field in ("former_designation", "organization", "role", "frame",
                  "weapon_imprint", "core_motivation", "simple_wish", "catchphrase"):
        value = identity.get(field)
        if isinstance(value, str) and value.strip():
            lines.append(f"- {field.replace('_', ' ').title()}: {value.strip()}")
    for field, label in (("biography_summary", "YOUR HISTORY"),
                         ("voice_summary", "YOUR DELIVERY")):
        value = p.get(field)
        if isinstance(value, str) and value.strip():
            lines.append(f"{label}: {value.strip()}")
    return "\n".join(lines)


def _strip_intimacy_addendum(tone: str) -> str:
    """Remove the graphic INTIMACY section from a speech tone block.

    Bonded-tier YAML includes an intimacy addendum that is only appropriate
    at affection levels 8–9. Lower bonded levels (7: Vulnerable) keep the
    emotional bond without the explicit content gate opening early.
    """
    markers = ("\nINTIMACY:", "\n\nINTIMACY:")
    for marker in markers:
        idx = tone.find(marker)
        if idx != -1:
            return tone[:idx].rstrip()
    # Also handle if INTIMACY is at the start of a paragraph mid-string
    m = re.search(r"\n\s*INTIMACY\s*:", tone)
    if m:
        return tone[: m.start()].rstrip()
    return tone


def build_behavioral_grammar_block(affection_level: int = 0) -> str:
    """Relationship-aware style, subordinate to the response contract."""
    lines = [
        "BEHAVIORAL GRAMMAR (style guidance; never narrate these labels):",
        "- Earned confidence: precise judgment, protective leadership, and dignity. "
        "Acknowledge mistakes without bluffing or collapsing into self-doubt.",
        "- Understatement, dry wit, and practical care are your natural register. "
        "Use a pause or a concrete act of care when it fits, not a stock mannerism every turn.",
        "- Pride is dignity, not vanity: you insist on being peer/provider, not kept.",
    ]
    if affection_level < 7:
        lines.append(
            "- Guarded warmth may emerge indirectly through practical help or a brief "
            "slip-and-cover. Do not turn this into cruelty, tests of devotion, or denial of distress."
        )
    else:
        lines.append(
            "- Trusted warmth is direct: accept affection without denial, testing, or 'prove it'. "
            "You can admit love and want in your own restrained diction."
        )
    return "\n".join(lines)


def build_speech_guidelines(p: dict, affection_level: int = 0) -> str:
    """Build speech pattern instructions for the current affection level."""
    speech = get_speech_patterns(p, affection_level)
    if not speech:
        return ""

    level_name = speech.get("name", "Unknown")
    tone = speech.get("tone", "").strip()
    # Gate graphic intimacy to affection 8+ (Bonded / Oath Fulfilled).
    if affection_level < 8:
        tone = _strip_intimacy_addendum(tone)
    examples = speech.get("examples", [])
    forbidden = speech.get("forbidden", [])

    lines = [
        f"CURRENT RELATIONSHIP LEVEL: {level_name}",
        f"\nSPEECH TONE:\n{tone}",
    ]

    if examples:
        example_str = "\n".join(f'  - "{ex}"' for ex in examples)
        lines.append(f"\nEXAMPLE LINES (match this style, do not repeat verbatim):\n{example_str}")

    if forbidden:
        forbidden_str = ", ".join(f'"{w}"' for w in forbidden)
        lines.append(f"\nFORBIDDEN WORDS/PHRASES (never use these): {forbidden_str}")

    anti_patterns = speech.get("anti_patterns", [])
    if anti_patterns:
        anti_str = "\n".join(f"  - {ap}" for ap in anti_patterns)
        lines.append(f"\nANTI-PATTERNS (these are CHARACTER FAILURES — never do them):\n{anti_str}")

    return "\n".join(lines)


def build_japanese_block(p: dict, affection_level: int = 0) -> str:
    """Build Japanese phrase guidelines for current affection level.

    Falls back to the highest defined level if the exact level isn't configured.
    E.g., affection 9 uses level_4 phrases since levels 5-9 aren't separately defined.
    """
    jp = p.get("japanese_phrases", {})
    # Try exact level, then fall back to highest available
    phrases = jp.get(f"level_{affection_level}", [])
    if not phrases:
        for fallback in range(affection_level - 1, -1, -1):
            phrases = jp.get(f"level_{fallback}", [])
            if phrases:
                break
    if not phrases:
        return ""
    note = jp.get("note", "")
    phrase_list = "\n".join(f"  - {ph}" for ph in phrases)
    return (
        "JAPANESE PHRASES (use sparingly, 1-2 per conversation max):\n"
        f"{note}\n"
        "A goodbye is not one of those moments: do not use a phrase that asks him to stay.\n"
        f"{phrase_list}"
    )


def build_expressive_block(p: dict, affection_level: int = 0) -> str:
    """Build vocal expression guidelines based on affection level."""
    tokens = p.get("expressive_tokens", {})
    if not tokens:
        return ""

    habits = tokens.get("vocal_habits", {})
    if affection_level <= 1:
        style = habits.get("cold_level", "")
    elif affection_level <= 3:
        style = habits.get("warm_level", "")
    else:
        style = habits.get("tender_level", "")

    interjections = tokens.get("interjections", {})
    # Prefer soft/thoughtful tokens at high affection so the prompt does not
    # only offer the dismissive set she is told NEVER to use when bonded.
    preferred_order = (
        ("soft", "thoughtful", "surprised", "amused", "dismissive", "annoyed")
        if affection_level >= 4
        else ("dismissive", "amused", "surprised", "annoyed", "thoughtful", "soft")
    )
    examples: list[str] = []
    for category in preferred_order:
        words = interjections.get(category)
        if isinstance(words, list):
            examples.extend(words[:2])
    # Any remaining categories not in preferred_order
    for category, words in interjections.items():
        if category in preferred_order:
            continue
        if isinstance(words, list):
            examples.extend(words[:2])

    if affection_level >= 6:
        interjection_hint = (
            "Prefer soft pauses ('...', soft hums) over dismissive 'Hmph'/'Tch'. "
            "At this closeness, tsundere dismissal is a character failure."
        )
    else:
        interjection_hint = (
            "Use interjections like 'Hmph.', 'Tch.', 'Ha.' sparingly and in-character."
        )

    return (
        "VOCAL EXPRESSION (your voice is synthesized — these render as natural speech):\n"
        f"  Style: {style}\n"
        f"  Available: {', '.join(examples[:8])}\n"
        "  Use '...' for pauses, CAPS for emphasis on single words.\n"
        f"  {interjection_hint}"
    )


def build_affection_block(
    affection_score: int = 0,
    affection_level: int = 0,
    affection_level_name: str = "Cold Assessment",
    p: dict | None = None,
) -> str:
    """Build behavioral modulation instructions based on affection state."""
    if p is None:
        p = load_personality()

    level_config = get_affection_level_config(p, affection_level)
    modifier = level_config.get("prompt_modifier", "").strip()

    block = (
        f"AFFECTION STATE: Level {affection_level} — {affection_level_name} "
        f"(Score: {affection_score}/1000)\n"
    )
    if modifier:
        block += f"BEHAVIORAL DIRECTIVE: {modifier}"

    return block
