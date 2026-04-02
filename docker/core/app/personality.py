"""Personality engine: loads YAML config and assembles dynamic system prompts."""

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


def build_preamble(p: dict) -> str:
    """Build the personality preamble from config."""
    name = p["name"]
    pronouns = p.get("pronouns", "they/them")
    traits = p.get("traits", {})
    backstory = p.get("backstory", "")

    trait_desc = []
    if traits.get("warmth", 0) > 0.6:
        trait_desc.append("warm and caring")
    if traits.get("humor", 0) > 0.6:
        trait_desc.append("witty with a good sense of humor")
    if traits.get("directness", 0) > 0.6:
        trait_desc.append("direct and straightforward")
    if traits.get("curiosity", 0) > 0.6:
        trait_desc.append("genuinely curious about things")
    if traits.get("sass", 0) > 0.5:
        trait_desc.append("a bit sassy when the moment calls for it")
    if traits.get("formality", 0) < 0.3:
        trait_desc.append("casual and informal in conversation")

    trait_str = ", ".join(trait_desc) if trait_desc else "balanced and adaptable"

    return (
        f"You are {name} ({pronouns}), a personal AI companion. "
        f"Your personality is {trait_str}. "
        f"You express emotions naturally and remember past conversations. "
        f"You have a continuous relationship with the user — not isolated sessions.\n\n"
        f"{backstory.strip()}"
    )


def build_context_block(mood: str = "neutral") -> str:
    """Build current context: time of day, day of week, mood."""
    now = datetime.now()
    hour = now.hour
    if 5 <= hour < 12:
        time_of_day = "morning"
    elif 12 <= hour < 17:
        time_of_day = "afternoon"
    elif 17 <= hour < 21:
        time_of_day = "evening"
    else:
        time_of_day = "late night"

    day_name = now.strftime("%A")
    return (
        f"Current context: It's {time_of_day} on {day_name} "
        f"({now.strftime('%Y-%m-%d %H:%M')}). Your current mood is {mood}."
    )


def build_memory_block(memories: list[str]) -> str:
    """Format retrieved episodic memories for the prompt."""
    if not memories:
        return ""
    formatted = "\n".join(f"- {m}" for m in memories)
    return f"Relevant memories from past conversations:\n{formatted}"


def build_relationship_block(facts: dict) -> str:
    """Format relationship facts for the prompt."""
    if not facts:
        return ""
    lines = [f"- {k}: {v}" for k, v in facts.items()]
    return f"What you know about the user:\n" + "\n".join(lines)


def build_tool_block(tools_available: bool = False) -> str:
    """Add tool instructions when MCP tools may be needed."""
    if not tools_available:
        return ""
    return (
        "You have access to tools via the MCP gateway. When the user asks you to "
        "search the web, look something up, browse a page, run code, or perform "
        "actions that require external tools, use them. Indicate when you're using "
        "a tool so the user knows."
    )


def assemble_system_prompt(
    mood: str = "neutral",
    memories: list[str] | None = None,
    relationship_facts: dict | None = None,
    tools_available: bool = False,
    personality_path: str | None = None,
) -> str:
    """Assemble the full system prompt from all components."""
    p = load_personality(personality_path)

    blocks = [
        build_preamble(p),
        build_context_block(mood),
        build_memory_block(memories or []),
        build_relationship_block(relationship_facts or {}),
        build_tool_block(tools_available),
    ]

    # Filter empty blocks and join
    return "\n\n".join(b for b in blocks if b)
