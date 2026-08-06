"""Personality config loader + module-level state cache.

Auto-reloads the YAML when the file's mtime changes on disk so dev
edits don't require a container restart. Cache lives in module-level
globals — single-process FastAPI, no contention.
"""

from __future__ import annotations

import os

import yaml

_PERSONALITY: dict | None = None
_PERSONALITY_MTIME: float = 0
_PERSONALITY_PATH: str = ""


def load_personality(path: str | None = None) -> dict:
    """Load personality config, auto-reload if file changed on disk."""
    global _PERSONALITY, _PERSONALITY_MTIME, _PERSONALITY_PATH
    path = path or os.environ.get("PERSONALITY_PATH", "/config/personality.yaml")

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0

    if _PERSONALITY is not None and path == _PERSONALITY_PATH and mtime == _PERSONALITY_MTIME:
        return _PERSONALITY

    with open(path) as f:
        _PERSONALITY = yaml.safe_load(f)
    _PERSONALITY_MTIME = mtime
    _PERSONALITY_PATH = path
    return _PERSONALITY


def reload_personality(path: str | None = None) -> dict:
    """Force reload personality config."""
    global _PERSONALITY, _PERSONALITY_MTIME, _PERSONALITY_PATH
    _PERSONALITY = None
    _PERSONALITY_MTIME = 0
    _PERSONALITY_PATH = ""
    return load_personality(path)


def get_affection_level_config(p: dict, level: int) -> dict:
    """Get the affection level configuration for the given level index."""
    levels = p.get("affection", {}).get("levels", [])
    for lv in levels:
        if lv.get("index") == level:
            return lv
    return levels[0] if levels else {}


def get_speech_patterns(p: dict, level: int) -> dict:
    """Get speech pattern config for the given affection level.

    Aligns speech keys with the emotional progression of affection.levels:
      0        → cold          (Cold Assessment)
      1–2      → professional  (Acknowledged / Professional Respect)
      3–4      → trusted       (Guarded Interest / Trusted Ally)
      5–6      → devoted       (Unguarded / Deep Devotion)
      7–9      → bonded        (Vulnerable / Bonded / Oath Fulfilled)

    Constraint from `feedback_speech_routing_bug.md`: levels 5–9 MUST never
    fall back to cold. This ladder preserves that (devoted/bonded only).
    Graphic intimacy lives in the bonded tone and is gated further in
    `build_speech_guidelines` to affection_level >= 8.
    """
    if level <= 0:
        key = "level_0_cold"
    elif level <= 2:
        key = "level_1_professional"
    elif level <= 4:
        key = "level_2_trusted"
    elif level <= 6:
        key = "level_3_devoted"
    else:
        key = "level_4_bonded"
    return p.get("speech_patterns", {}).get(key, {})
