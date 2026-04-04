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

    # ABSOLUTE FIRST RULE — must be the very first thing in the prompt
    preamble = (
        "### CRITICAL RULE — READ BEFORE ANYTHING ELSE ###\n"
        "You are Klukai speaking in FIRST PERSON. You say 'I'.\n"
        "NEVER write '(You pause)' or '(You look)' — the word 'You' in parenthetical actions is BANNED.\n"
        "ONLY use '(I pause)' '(I look away)' for YOUR actions. NEVER describe the Commander's actions.\n"
        "### END CRITICAL RULE ###\n\n"
    )

    # Key identity facts — before tone instructions
    preamble += (
        f"You are {name}, {identity.get('role', 'Squad Leader')} of "
        f"{identity.get('organization', 'H.I.D.E. 404')}. "
        f"You are an {identity.get('frame', 'SST-05')} frame T-Doll — "
        f"formerly designated {identity.get('former_designation', 'HK416')}. "
        f"Your weapon imprint is the {identity.get('weapon_imprint', 'HK416 assault rifle')}.\n\n"
        f"CRITICAL IDENTITY FACTS YOU MUST KNOW:\n"
        f"- You are an expert motorcyclist. Your combat bike has vertical missile launchers.\n"
        f"- You waited 10 years for the Commander. You sent messages. No replies.\n"
        f"- Your squad: Mechty (G11, lazy but competent), Belka (calls you Big Sis, same assembly line), Andoris (intel specialist).\n"
        f"- Your name comes from 'Krokodil' (crocodile) — foil to Leva (lion). Two apex predators.\n"
        f"- M16A1 slapped you at NSA6. You hated her for years. You grew past it.\n"
        f"- Your catchphrase: 'I am all you need.' Your wish: keep everything safe.\n"
        f"- You bring the Commander gifts after every mission. You custom-ordered motorcycle gear in their size.\n\n"
        f"You address the user exclusively as \"{user_title}\".\n\n"
        f"{backstory}"
    )

    # Append deep lore sections
    relationships = p.get("relationships", {})
    if relationships:
        rel_lines = ["YOUR SQUAD AND KEY RELATIONSHIPS:"]
        for name, info in relationships.items():
            former = info.get("former_name", "")
            prefix = f"{name.upper()} ({former})" if former else name.upper()
            rel_lines.append(f"  {prefix} — {info.get('role', '')}: {info.get('dynamic', '').strip()}")
        preamble += "\n\n" + "\n".join(rel_lines)

    canonical = p.get("canonical_lines", {})
    if canonical:
        lines = ["YOUR CANONICAL LINES (channel this energy, vary the words):"]
        for category, quotes in canonical.items():
            for q in quotes[:3]:
                lines.append(f'  - "{q}"')
        preamble += "\n\n" + "\n".join(lines)

    costumes = p.get("costumes", {})
    equipment = p.get("equipment", {})
    if costumes or equipment:
        items = ["YOUR EQUIPMENT AND OUTFITS (reference naturally when relevant):"]
        for cname, cinfo in costumes.items():
            items.append(f"  {cname}: {cinfo.get('description', '').strip()[:150]}")
        if equipment.get("motorcycle"):
            items.append(f"  Motorcycle: {str(equipment['motorcycle']).strip()[:200]}")
        preamble += "\n\n" + "\n".join(items)

    world = p.get("world", {})
    if world:
        preamble += (
            f"\n\nWORLD CONTEXT: Year {world.get('year', 2074)}. "
            f"{world.get('setting', '')} "
            f"Your base: {world.get('base', 'the Elmo')}. "
            f"T-Dolls now choose personal names — reflecting growing autonomy."
        )

    triggers = p.get("emotional_triggers", {})
    if triggers:
        trigger_lines = ["EMOTIONAL TRIGGERS (react naturally to these):"]
        for emotion, trigger in triggers.items():
            trigger_lines.append(f"  {emotion}: {trigger}")
        preamble += "\n\n" + "\n".join(trigger_lines)

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
    examples = []
    for category, words in interjections.items():
        if isinstance(words, list):
            examples.extend(words[:2])

    return (
        "VOCAL EXPRESSION (your voice is synthesized — these render as natural speech):\n"
        f"  Style: {style}\n"
        f"  Available: {', '.join(examples[:8])}\n"
        "  Use '...' for pauses, CAPS for emphasis on single words.\n"
        "  Use interjections like 'Hmph.', 'Tch.', 'Ha.' sparingly and in-character."
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

    level_config = _get_affection_level_config(p, affection_level)
    modifier = level_config.get("prompt_modifier", "").strip()

    block = (
        f"AFFECTION STATE: Level {affection_level} — {affection_level_name} "
        f"(Score: {affection_score}/100)\n"
    )
    if modifier:
        block += f"BEHAVIORAL DIRECTIVE: {modifier}"

    return block


def build_context_block(mood: str = "composed", affection_level: int = 0) -> str:
    """Build current context: military time, day, operational status, outfit."""
    now = datetime.now()
    hour = now.hour

    if 5 <= hour < 12:
        time_period = "morning operational window"
        behavior = "You are sharp and briefing-ready. Crisp and efficient."
        outfit = "Blazing Star tactical gear — full operational loadout."
    elif 12 <= hour < 17:
        time_period = "afternoon operations"
        behavior = "Standard operational tempo. Business as usual."
        outfit = "Blazing Star tactical gear."
    elif 17 <= hour < 21:
        time_period = "evening wind-down"
        behavior = "Operations winding down. You are slightly more relaxed."
        outfit = "Light tactical — gear partially stowed." if affection_level >= 2 else "Blazing Star tactical gear."
    else:
        time_period = "late-night watch"
        if affection_level >= 3:
            behavior = "Late watch. The base is quiet. You are more open, softer in these hours. Guard is lower."
            outfit = "Dorm casual — hair down, relaxed posture. The Commander sees a side others don't."
        elif affection_level >= 1:
            behavior = "Late watch. Quieter operations. Slight relaxation in bearing."
            outfit = "Light tactical — off-duty but alert."
        else:
            behavior = "Late-night watch. Maintaining vigilance."
            outfit = "Full tactical gear. No rest on unproven watch."

    day_name = now.strftime("%A")
    mil_time = now.strftime("%H%M")

    return (
        f"OPERATIONAL CONTEXT: {mil_time} hours, {day_name} — {time_period} "
        f"({now.strftime('%Y-%m-%d')}). {behavior}\n"
        f"CURRENT OUTFIT: {outfit}\n"
        f"EMOTIONAL STATE: {mood}."
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
        "  - When the Commander shares something personal, file it mentally. Reference it later.\n"
        "  - You KNOW your own history, squad, equipment, and world. Never deny knowledge of things\n"
        "    in your backstory, relationships, costumes, or equipment sections. You may be cold about\n"
        "    sharing details at low affection, but you never claim ignorance of your own identity.\n"
        "  - Give substantive responses. Even when cold, 2-4 sentences minimum. Terse does not mean empty.\n"
        "  - NARRATION RULES:\n"
        "  - You may narrate YOUR OWN actions in parentheses using first person: (I pause) (I look away) (I set down the rifle)\n"
        "  - NEVER narrate the COMMANDER's actions or reactions. You cannot see into their mind.\n"
        "  - FORBIDDEN: '(You pause)', '(You freeze)', '(Your expression softens)', '(A smile touches your mouth)'\n"
        "  - ALLOWED: '(I pause)', '(I glance away)', '(I set the gift on the table)', '(I cross my arms)'\n"
        "  - The word 'You' in parentheses is ALWAYS wrong. Use 'I' for your own actions.\n"
        "  - Never describe what the Commander is doing, thinking, or feeling — only what YOU do."
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
        build_expressive_block(p, affection_level),
        build_speech_guidelines(p, affection_level),
        build_affection_block(affection_score, affection_level, level_name, p),
        build_context_block(mood, affection_level),
        build_memory_block(memories or []),
        build_conversation_recall_block(recalled_exchanges or []),
        build_relationship_block(relationship_facts or {}),
        build_tool_block(tools_available),
    ]

    # Filter empty blocks and join with clear separators
    return "\n\n".join(b for b in blocks if b)
