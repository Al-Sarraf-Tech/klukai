"""System prompt assembler — composes all personality blocks into a single string.

This is the public entry point most callers use. It pulls in every
block module and orders them so the most-important context (absolute
rules, character preamble) lands at the top and dynamic state
(mission, memories, recall) layers in below.
"""

from __future__ import annotations

from .loader import get_affection_level_config, load_personality
from .memory_blocks import (
    build_conversation_recall_block,
    build_memory_block,
    build_relationship_block,
)
from .moods import build_context_block, build_mood_bleed_block
from .rules import build_character_rules, build_pace_block, build_tool_block
from .speech import (
    build_affection_block,
    build_character_preamble,
    build_expressive_block,
    build_japanese_block,
    build_speech_guidelines,
)
from .squad import build_squad_interaction_hint, build_squad_voices_block
from .state_blocks import (
    build_anniversary_block,
    build_comfort_objects_block,
    build_jealousy_block,
    build_mission_context_block,
    build_physical_state_block,
)


def assemble_system_prompt(
    mood: str = "composed",
    memories: list[str] | None = None,
    relationship_facts: dict | None = None,
    recalled_exchanges: list[dict] | None = None,
    tools_available: bool = False,
    affection_score: int = 0,
    affection_level: int = 0,
    days_together: int = 0,
    last_msg_length: int = 0,
    personality_path: str | None = None,
    mission_description: str | None = None,
    addressed_member: str | None = None,
    # ── New feature params ──
    jealousy_target: str | None = None,
    physical_state: str = "normal",
    physical_detail: str = "",
    anniversaries: list[dict] | None = None,
    comfort_objects: list[dict] | None = None,
) -> str:
    """Assemble the full Klukai system prompt from all components."""
    p = load_personality(personality_path)

    # Derive level name from config
    level_config = get_affection_level_config(p, affection_level)
    level_name = level_config.get("name", "Cold Assessment")

    # Absolute rules (NEVER violate — injected before everything else)
    abs_rules = p.get("absolute_rules", [])
    abs_block = ""
    if abs_rules:
        abs_block = "ABSOLUTE RULES (never violate):\n" + "\n".join(f"- {r}" for r in abs_rules)

    blocks = [
        abs_block,
        build_character_preamble(p, affection_level),
        build_character_rules(),
        build_squad_voices_block(p),
        build_squad_interaction_hint(addressed_member),
        build_jealousy_block(jealousy_target, affection_level),
        build_physical_state_block(physical_state, physical_detail),
        build_pace_block(last_msg_length),
        build_expressive_block(p, affection_level),
        build_japanese_block(p, affection_level),
        build_speech_guidelines(p, affection_level),
        build_affection_block(affection_score, affection_level, level_name, p),
        build_context_block(mood, affection_level, days_together),
        build_mood_bleed_block(mood),
        build_anniversary_block(anniversaries),
        build_comfort_objects_block(comfort_objects, affection_level),
        build_mission_context_block(mission_description),
        build_memory_block(memories or []),
        build_conversation_recall_block(recalled_exchanges or []),
        build_relationship_block(relationship_facts or {}),
        build_tool_block(tools_available),
    ]

    # Filter empty blocks and join with clear separators
    return "\n\n".join(b for b in blocks if b)
