"""Scene detection and prompt composition; independent of rendering clients."""

from __future__ import annotations

import re

from app.image_gen_constants import (
    AFFECTION_MOOD_TAGS,
    COMMANDER_DEFAULT_OUTFIT,
    COMMANDER_IDENTITY,
    COMMANDER_OUTFIT_MAP,
    COUPLE_KEYWORDS,
    COUPLE_TAGS,
    IMAGE_KEYWORDS,
    KLUKAI_DEFAULT_OUTFIT,
    KLUKAI_IDENTITY,
    KLUKAI_LORA_TRIGGER,
    LANDSCAPE_KEYWORDS,
    MISSION_SCENE_TAGS,
    MOOD_EXPRESSION_TAGS,
    NEGATIVE_TAGS,
    OUTFIT_MAP,
    INTIMATE_MIN_LEVEL,
    INTIMATE_OUTFIT_KEYS,
    QUALITY_TAGS,
    SCENE_OUTFIT_KEYWORDS,
    SFW_NEGATIVE_TAGS,
    SQUAD_KEYWORDS,
    TIME_OF_DAY_TAGS,
)


def detect_squad_members(text: str) -> list[str]:
    """Detect which squad members are mentioned in the text."""
    lower = text.lower()
    found = []
    for name in SQUAD_KEYWORDS:
        if name in lower:
            found.append(name)
    return found


def build_mission_prompt(
    scene_type: str = "combat",
    squad_members: list[str] | None = None,
    injuries: list[str] | None = None,
    affection_level: int = 0,
) -> str:
    """Build an image prompt for mission-context scenes.

    Args:
        scene_type: Key from MISSION_SCENE_TAGS.
        squad_members: List of squad member names to include.
        injuries: Active injury events (e.g., ["klukai_injured", "squad_injured"]).
        affection_level: 0-9, affects Klukai's expression.
    """
    parts = [QUALITY_TAGS, KLUKAI_LORA_TRIGGER]

    # Klukai is always in mission images
    parts.append(KLUKAI_IDENTITY)
    parts.append(
        "high ponytail, tactical gear, body armor, combat vest, rifle, intense"
    )

    # Add injury tags to Klukai if she's hurt
    if injuries and "klukai_injured" in injuries:
        parts.append(
            "bandaged arm, torn sleeve, blood stains, determined expression, still commanding"
        )

    # Add squad members
    if squad_members:
        for name in squad_members[:3]:  # Max 3 extra characters to avoid crowding
            if name in SQUAD_KEYWORDS:
                parts.append(SQUAD_KEYWORDS[name])
        # Add injury tags to generic squad if injured
        if injuries and "squad_injured" in injuries:
            parts.append("injured teammate, field bandages, supporting each other")
        if injuries and "medical_emergency" in injuries:
            parts.append("field medic, treating wounds, urgent medical attention")

    # Scene tags
    scene = MISSION_SCENE_TAGS.get(scene_type, MISSION_SCENE_TAGS["combat"])
    parts.append(scene)

    # Multi-girl tag if squad members present
    if squad_members:
        count = len(squad_members) + 1  # +1 for Klukai
        parts.append(f"{count}girls, multiple girls, group")

    return ", ".join(parts)


def needs_image(message: str) -> bool:
    """Check if the message is requesting image generation."""
    lower = message.lower()
    return any(kw in lower for kw in IMAGE_KEYWORDS)


def is_couple_scene(text: str) -> bool:
    """Detect if the request is for a scene with both Klukai and the Commander."""

    lower = text.lower()
    return any(
        re.search(r"\b" + re.escape(kw) + r"\b", lower) for kw in COUPLE_KEYWORDS
    )


def is_landscape(text: str) -> bool:
    """Detect if the scene should use landscape aspect ratio."""
    lower = text.lower()
    return any(kw in lower for kw in LANDSCAPE_KEYWORDS)


def _select_outfit(context: str, outfit_map: dict[str, str], default: str) -> str:
    """Pick the best outfit from a map based on conversation context keywords."""
    lower = context.lower()
    for keyword, outfit in outfit_map.items():
        if keyword in lower:
            return outfit
    return default


def is_outfit_unlocked(costume: str, affection_level: int) -> bool:
    """Return whether ``costume`` is unlocked at the given affection level.

    Delegates to the wardrobe catalog (app/wardrobe.py). Legacy ids map
    forward; an unknown id is locked (fail-closed).
    """
    from . import wardrobe

    return wardrobe.is_unlocked(costume, affection_level)


def _explicit_scene(text: str) -> bool:
    """An explicit bath/bed/lingerie scene (whole words), which outranks today's outfit."""
    lower = text.lower()
    return any(re.search(rf"\b{kw}\b", lower) for kw in SCENE_OUTFIT_KEYWORDS)


def _outfit_map_for(affection_level: int) -> dict[str, str]:
    """OUTFIT_MAP minus the intimate outfits below the intimacy gate."""
    if affection_level >= INTIMATE_MIN_LEVEL:
        return OUTFIT_MAP
    return {k: v for k, v in OUTFIT_MAP.items() if k not in INTIMATE_OUTFIT_KEYS}


def build_prompt(
    scene_tags: str,
    couple: bool = False,
    affection_level: int = 0,
    context: str = "",
    squad_members: list[str] | None = None,
    mood: str = "composed",
    time_of_day: str | None = None,
    costume: str | None = None,
    request: str | None = None,
    affection_tags: bool = True,
) -> str:
    """Build the full positive prompt with quality tags, LoRA trigger, and character identities.

    Args:
        scene_tags: Scene/action Danbooru tags.
        couple: Whether to include the Commander.
        affection_level: 0-9, affects mood expression tags.
        context: Recent conversation text for outfit selection.
        squad_members: Optional list of squad member names to include in the scene.
        mood: Session mood (e.g. "tender", "playful") — adds a concise expression
            cue before the scene tags. Defaults to "composed". Unknown moods are
            ignored (no descriptor injected).
        time_of_day: One of morning/afternoon/evening/night, or None. When set,
            injects a short lighting/time cue before the scene tags.
        costume: Optional wardrobe id (app/wardrobe.py catalog) — what she is
            wearing. Its tag block replaces the keyword-matched outfit, unless
            the scene is explicitly a bath/bed/lingerie one. Unknown ids fall
            through to the keyword-context outfit logic.
        request: His own words for this image, when known. Only these (never
            her replies in ``context``) can make it an explicit bath/bed scene.
        affection_tags: Inject the affection-level expression/setting tags.
            Live Portrait turns this off: its frames set the expression
            themselves and need a neutral, setting-free base.
    """
    parts = [QUALITY_TAGS, KLUKAI_LORA_TRIGGER]

    # Add affection-aware mood tags
    mood_tags = AFFECTION_MOOD_TAGS.get(affection_level, "") if affection_tags else ""
    if mood_tags:
        parts.append(mood_tags)

    # Scene-aware descriptors — concise mood expression + time/lighting cue,
    # injected BEFORE the scene tags so they colour the moment without bloat.
    expression = MOOD_EXPRESSION_TAGS.get(mood or "")
    if expression:
        parts.append(expression)
    if time_of_day:
        lighting = TIME_OF_DAY_TAGS.get(time_of_day)
        if lighting:
            parts.append(lighting)

    # Context for outfit matching: use full context (conversation + scene tags)
    outfit_context = f"{context} {scene_tags}" if context else scene_tags
    # What she is wearing today overrides the keyword-matched outfit, except
    # for an explicit bath/bed scene; otherwise fall back to keyword selection.
    from . import wardrobe

    costume_tags = wardrobe.image_tags(costume) if costume else None
    explicit = affection_level >= INTIMATE_MIN_LEVEL and _explicit_scene(
        request if request is not None else outfit_context
    )
    if costume_tags and not explicit:
        klukai_outfit = costume_tags
    else:
        klukai_outfit = _select_outfit(
            outfit_context, _outfit_map_for(affection_level), KLUKAI_DEFAULT_OUTFIT
        )

    if couple:
        commander_outfit = _select_outfit(
            outfit_context, COMMANDER_OUTFIT_MAP, COMMANDER_DEFAULT_OUTFIT
        )
        parts.append(COUPLE_TAGS)
        parts.append(f"{COMMANDER_IDENTITY}, {commander_outfit}")
        parts.append(KLUKAI_IDENTITY)
    else:
        parts.append(KLUKAI_IDENTITY)
    parts.append(klukai_outfit)

    # Add squad members if specified
    if squad_members:
        for name in squad_members[:3]:  # Max 3 to avoid overcrowding
            if name in SQUAD_KEYWORDS:
                parts.append(SQUAD_KEYWORDS[name])
        total_girls = 1 + len([n for n in squad_members if n in SQUAD_KEYWORDS])
        if total_girls > 1:
            parts.append(f"{total_girls}girls, multiple girls")

    parts.append(scene_tags)
    return ", ".join(parts)


def negative_prompt(sfw: bool = False, extra: str | None = None) -> str:
    """The negative prompt; ``sfw`` adds the below-intimacy-gate backstop and
    ``extra`` a caller's own negatives."""
    base = f"{NEGATIVE_TAGS}, {SFW_NEGATIVE_TAGS}" if sfw else NEGATIVE_TAGS
    return f"{base}, {extra}" if extra else base


def lighting_period(hour: int) -> str:
    """Map local wall-clock hours to the established scene-lighting periods."""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"
