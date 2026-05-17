"""Personality engine — loads Klukai YAML config and assembles affection-modulated system prompts.

This package replaces the original monolithic `app/personality.py` (797 LOC, exceeded
A-tier <500 LOC threshold). It is split for S+ uplift Phase 2 with the same public API:

    from app.personality import load_personality, assemble_system_prompt, ...

Internal layout:
- :mod:`.loader`         — YAML loading + mtime-based cache invalidation
- :mod:`.speech`         — character preamble + speech/japanese/expressive/affection blocks
- :mod:`.moods`          — 48 moods, 6 categories, per-mood coloring, context block
- :mod:`.memory_blocks`  — memory / relationship / recall blocks
- :mod:`.squad`          — supporting-cast voice profiles + squad interaction hints
- :mod:`.state_blocks`   — physical / jealousy / anniversary / comfort / mission blocks
- :mod:`.rules`          — absolute character rules + pace + tool blocks
- :mod:`.system_prompt`  — assemble_system_prompt() entry point
"""

from __future__ import annotations

# Loader API
from .loader import (
    get_affection_level_config,
    get_speech_patterns,
    load_personality,
    reload_personality,
)

# Memory/recall blocks
from .memory_blocks import (
    build_conversation_recall_block,
    build_memory_block,
    build_relationship_block,
)

# Mood data + blocks
from .moods import (
    CATEGORY_BLEED_RULES,
    MOOD_CATEGORIES,
    MOOD_SPECIFIC_BLEED,
    build_context_block,
    build_mood_bleed_block,
)

# Rules + pace + tool
from .rules import (
    build_character_rules,
    build_pace_block,
    build_tool_block,
)

# Speech blocks
from .speech import (
    build_affection_block,
    build_character_preamble,
    build_expressive_block,
    build_japanese_block,
    build_speech_guidelines,
)

# Squad
from .squad import (
    build_squad_interaction_hint,
    build_squad_voices_block,
)

# State blocks
from .state_blocks import (
    build_anniversary_block,
    build_comfort_objects_block,
    build_crown_jewel_block,
    build_jealousy_block,
    build_mission_context_block,
    build_physical_state_block,
)

# Main entry point
from .system_prompt import assemble_system_prompt

# Back-compat: the underscore-prefixed names that internal code used.
# Re-exported so any caller doing `from app.personality import _get_affection_level_config`
# (if any) still works during the transition.
_get_affection_level_config = get_affection_level_config
_get_speech_patterns = get_speech_patterns


__all__ = [
    # Loader
    "load_personality",
    "reload_personality",
    "get_affection_level_config",
    "get_speech_patterns",
    "_get_affection_level_config",  # back-compat alias
    "_get_speech_patterns",  # back-compat alias
    # Speech
    "build_character_preamble",
    "build_speech_guidelines",
    "build_japanese_block",
    "build_expressive_block",
    "build_affection_block",
    # Moods
    "MOOD_CATEGORIES",
    "MOOD_SPECIFIC_BLEED",
    "CATEGORY_BLEED_RULES",
    "build_mood_bleed_block",
    "build_context_block",
    # Memory blocks
    "build_memory_block",
    "build_relationship_block",
    "build_conversation_recall_block",
    # Squad
    "build_squad_voices_block",
    "build_squad_interaction_hint",
    # State blocks
    "build_physical_state_block",
    "build_jealousy_block",
    "build_anniversary_block",
    "build_comfort_objects_block",
    "build_crown_jewel_block",
    "build_mission_context_block",
    # Rules + pace + tool
    "build_character_rules",
    "build_pace_block",
    "build_tool_block",
    # System prompt
    "assemble_system_prompt",
]
