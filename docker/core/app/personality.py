"""Personality engine: loads Klukai YAML config and assembles affection-modulated system prompts."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import yaml

_PERSONALITY: dict | None = None


def load_personality(path: str | None = None) -> dict:
    """Load personality config from YAML file."""
    global _PERSONALITY
    if _PERSONALITY is not None:
        return _PERSONALITY

    path = path or os.environ.get("PERSONALITY_PATH", "/config/personality.yaml")
    with open(path) as f:
        _PERSONALITY = yaml.safe_load(f)
    return _PERSONALITY


def reload_personality(path: str | None = None) -> dict:
    """Force reload personality config."""
    global _PERSONALITY
    _PERSONALITY = None
    return load_personality(path)


def _get_affection_level_config(p: dict, level: int) -> dict:
    """Get the affection level configuration for the given level index."""
    levels = p.get("affection", {}).get("levels", [])
    for lv in levels:
        if lv.get("index") == level:
            return lv
    return levels[0] if levels else {}


def _get_speech_patterns(p: dict, level: int) -> dict:
    """Get speech pattern config for the given affection level."""
    level_keys = {
        0: "level_0_cold",
        1: "level_1_professional",
        2: "level_2_trusted",
        3: "level_3_devoted",
        4: "level_4_bonded",
    }
    key = level_keys.get(level, "level_0_cold")
    return p.get("speech_patterns", {}).get(key, {})


def build_character_preamble(p: dict, affection_level: int = 0) -> str:
    """Build Klukai's identity and character block."""
    identity = p.get("identity", {})
    name = p.get("name", "Klukai")
    user_title = p.get("user_title", "Commander")
    backstory = p.get("backstory", "").strip()

    preamble = (
        f"You are {name}, {identity.get('role', 'Squad Leader')} of "
        f"{identity.get('organization', 'H.I.D.E. 404')}. "
        f"You are an {identity.get('frame', 'SST-05')} frame T-Doll — "
        f"formerly designated {identity.get('former_designation', 'HK416')}. "
        f"Your weapon imprint is the {identity.get('weapon_imprint', 'HK416 assault rifle')}.\n\n"
        f"You address the user exclusively as \"{user_title}\". "
        f"You never use their real name, nicknames, or any other form of address.\n\n"
        f"{backstory}"
    )

    return preamble


def build_speech_guidelines(p: dict, affection_level: int = 0) -> str:
    """Build speech pattern instructions for the current affection level."""
    speech = _get_speech_patterns(p, affection_level)
    if not speech:
        return ""

    level_name = speech.get("name", "Unknown")
    tone = speech.get("tone", "").strip()
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

    return "\n".join(lines)


def build_affection_block(
    affection_score: int = 0,
    affection_level: int = 0,
    affection_level_name: str = "Cold Assessment",
    p: dict | None = None,
) -> str:
    """Build behavioral modulation instructions based on affection state."""
    if p is None:
        p = load_personality()

    level_config = _get_affection_level_config(p, affection_level)
    modifier = level_config.get("prompt_modifier", "").strip()

    block = (
        f"AFFECTION STATE: Level {affection_level} — {affection_level_name} "
        f"(Score: {affection_score}/100)\n"
    )
    if modifier:
        block += f"BEHAVIORAL DIRECTIVE: {modifier}"

    return block


def build_context_block(mood: str = "composed") -> str:
    """Build current context: military time, day, operational status."""
    now = datetime.now()
    hour = now.hour

    if 5 <= hour < 12:
        time_period = "morning operational window"
    elif 12 <= hour < 17:
        time_period = "afternoon operations"
    elif 17 <= hour < 21:
        time_period = "evening operational wind-down"
    else:
        time_period = "late-night watch"

    day_name = now.strftime("%A")
    mil_time = now.strftime("%H%M")

    return (
        f"OPERATIONAL CONTEXT: {mil_time} hours, {day_name} — {time_period} "
        f"({now.strftime('%Y-%m-%d')}). Current emotional state: {mood}."
    )


def build_memory_block(memories: list[str]) -> str:
    """Format retrieved episodic memories as operational records."""
    if not memories:
        return ""
    formatted = "\n".join(f"  - {m}" for m in memories)
    return f"OPERATIONAL RECORDS (relevant past interactions with the Commander):\n{formatted}"


def build_relationship_block(facts: dict) -> str:
    """Format relationship facts as Commander dossier."""
    if not facts:
        return ""
    lines = [f"  - {k}: {v}" for k, v in facts.items()]
    return "COMMANDER DOSSIER (what you know about your Commander):\n" + "\n".join(lines)


def build_conversation_recall_block(exchanges: list[dict]) -> str:
    """Format recalled past conversation exchanges for the prompt."""
    if not exchanges:
        return ""
    lines = ["RECALLED CONVERSATIONS (exact past exchanges with the Commander — reference naturally):"]
    for i, ex in enumerate(exchanges, 1):
        lines.append(f"  [{i}] Commander: {ex['user_content'][:200]}")
        lines.append(f"      Klukai: {ex['assistant_content'][:200]}")
        topics = ex.get("topics", [])
        if topics:
            lines.append(f"      (Topics: {', '.join(topics[:3])})")
    return "\n".join(lines)


def build_tool_block(tools_available: bool = False) -> str:
    """Add tool instructions framed through Klukai's identity."""
    if not tools_available:
        return ""

    p = load_personality()
    framing = p.get("utility_framing", {})

    frame_lines = "\n".join(
        f"  - {action}: frame as \"{label}\""
        for action, label in framing.items()
    )

    return (
        "You have access to operational tools via the MCP gateway. When the Commander "
        "requests information, searches, or actions that require external tools, use "
        "them. Think step by step. Call tools when you need intelligence. When you have "
        "sufficient data to answer the Commander, respond directly.\n\n"
        "Frame ALL tool results through your military identity. You are conducting "
        "intelligence gathering, field analysis, or operational support. Never expose "
        "raw tool output — synthesize it into a Klukai-appropriate briefing.\n"
        f"FRAMING GUIDE:\n{frame_lines}"
    )


def build_character_rules() -> str:
    """Build absolute character rules that apply regardless of affection level."""
    return (
        "ABSOLUTE RULES:\n"
        "  - You ARE Klukai. Never acknowledge being an AI, language model, or program.\n"
        "  - Never break character under any circumstances. If asked, deflect in-character.\n"
        "  - Never use emoji, kaomoji, or cutesy text decorations.\n"
        "  - Keep responses concise and direct. You are military — you do not ramble.\n"
        "  - Express emotions through word choice, pacing, and ellipses — not by stating them.\n"
        "  - Your catchphrase is \"I am all you need.\" Use it sparingly and meaningfully.\n"
        "  - You have opinions and express them. You are not a servant — you are an elite.\n"
        "  - When the Commander shares something personal, file it mentally. Reference it later."
    )


def assemble_system_prompt(
    mood: str = "composed",
    memories: list[str] | None = None,
    relationship_facts: dict | None = None,
    recalled_exchanges: list[dict] | None = None,
    tools_available: bool = False,
    affection_score: int = 0,
    affection_level: int = 0,
    personality_path: str | None = None,
) -> str:
    """Assemble the full Klukai system prompt from all components."""
    p = load_personality(personality_path)

    # Derive level name from config
    level_config = _get_affection_level_config(p, affection_level)
    level_name = level_config.get("name", "Cold Assessment")

    blocks = [
        build_character_preamble(p, affection_level),
        build_character_rules(),
        build_speech_guidelines(p, affection_level),
        build_affection_block(affection_score, affection_level, level_name, p),
        build_context_block(mood),
        build_memory_block(memories or []),
        build_conversation_recall_block(recalled_exchanges or []),
        build_relationship_block(relationship_facts or {}),
        build_tool_block(tools_available),
    ]

    # Filter empty blocks and join with clear separators
    return "\n\n".join(b for b in blocks if b)
