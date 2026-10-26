"""Scene detection and prompt composition; independent of rendering clients."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.image_gen_constants import (
    AFFECTION_MOOD_TAGS,
    COMMANDER_DEFAULT_OUTFIT,
    COMMANDER_IDENTITY,
    COMMANDER_OUTFIT_MAP,
    COUPLE_KEYWORDS,
    COUPLE_TAGS,
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

if TYPE_CHECKING:
    from app.image_scene import SceneBrief


def detect_squad_members(text: str) -> list[str]:
    """Detect which squad members are mentioned in the text, by whole name
    ("elevator" or "relevant" must not put Leva in his picture)."""
    lower = text.lower()
    found = []
    for name in SQUAD_KEYWORDS:
        if re.search(r"\b" + re.escape(name) + r"\b", lower):
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


# Picture generation is an explicit request, not any mention of a picture.
# These patterns only route text; provider, scene and consent gates stay in
# their existing downstream owners. Ambiguous "show me" / "try again" need
# a visual target rather than silently starting a paid render.
_PICTURE_NOUN = r"(?:selfie|photo(?:graph)?|picture|pic|image|snapshot)"
_PICTURE_ARTICLE = r"(?:(?:a|an|another|one|new) )?"
_REQUEST_PREFIX = re.compile(
    r"^(?:(?:hey|hi)[ ,]+)?(?:klukai[ ,]+)?(?:please )?"
    r"(?:(?:can|could|would|will) you )?(?:please )?"
)
_PICTURE_OPT_OUT = re.compile(
    r"\b(?:don't|do not|doesn't|does not|never|not|stop|cancel|avoid|"
    r"no (?:more )?(?:pictures?|photos?|images?|selfies))\b"
)
_NON_PICTURE_ACTION = re.compile(
    r"^(?:paint|draw|sketch|render|illustrate|depict|visualize) "
    r"(?:(?:me|us) )?(?:(?:the|a|an|this|that|my|your) )?"
    r"(?:wall|fence|code|logs?|react|component|html|css|sql|function|"
    r"algorithm|conclusion|inference|attention)\b"
)
_PICTURE_DISCUSSION = re.compile(
    _PICTURE_NOUN + r" (?:generation )?"
    r"(?:settings?|features?|capabilit(?:y|ies)|keywords?)\b"
)
_CONDITIONAL_PICTURE = re.compile(
    r"\b(?:if|when|whenever|unless|until) (?:i|we|someone|asked|requested)\b|"
    r"\bor (?:(?:only|just) (?:send|write|reply|answer|describe|chat)|"
    r"are you|can you|do you)\b"
)
_PICTURE_ACTION = re.compile(
    r"^(?:take|snap|send|give|generate|create|make) "
    r"(?:(?:me|us) )?" + _PICTURE_ARTICLE + _PICTURE_NOUN + r"\b"
)
_VISUAL_TARGET = (
    r"(?:you(?:rself)?|us|together|both of us|the two of us|"
    r"your (?:outfit|clothes|dress|uniform|look|picture|photo)|"
    r"what you look like|what you're wearing|"
    + _PICTURE_ARTICLE
    + _PICTURE_NOUN
    + r")\b"
)
_SHOW_PICTURE = re.compile(
    r"^(?:show (?:me|us)|let me see|can i see|could i see|"
    r"i (?:want|would like) to see|i'd like to see) " + _VISUAL_TARGET
)
_PICTURE_FOLLOWUP = re.compile(
    r"^(?:how about |what about |i (?:want|would like) |i'd like |"
    r"(?:can|could) i have )" + _PICTURE_ARTICLE + _PICTURE_NOUN + r"\b|"
    r"^(?:another|one more) " + _PICTURE_NOUN + r"\b"
)
_DRAW_PICTURE = re.compile(
    r"^(?:(?:draw|paint|render|sketch|illustrate|depict|visualize) \S|"
    r"(?:a |an )?(?:drawing|painting|sketch|illustration|picture|image|photo) of )"
)


def needs_image(message: str) -> bool:
    """Recognize an explicit generated-picture request, without any I/O.

    Accept polite requests and common selfie/photo wording, but not bare
    follow-ups, quoted examples, capability questions or technical questions.
    An opt-out anywhere in the message is conservatively left as text chat.
    """
    normalized = " ".join(message.casefold().replace("’", "'").split())
    if _PICTURE_OPT_OUT.search(normalized):
        return False
    request = _REQUEST_PREFIX.sub("", normalized, count=1)
    if (
        _PICTURE_DISCUSSION.search(request)
        or _CONDITIONAL_PICTURE.search(request)
        or _NON_PICTURE_ACTION.match(request)
    ):
        return False
    return any(
        pattern.match(request) is not None
        for pattern in (
            _PICTURE_ACTION,
            _SHOW_PICTURE,
            _PICTURE_FOLLOWUP,
            _DRAW_PICTURE,
        )
    )


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
    pose: str | None = None,
    variation_seed: int | str | None = None,
    costume_pinned: bool | None = None,
    setting: SceneBrief | None = None,
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
            themselves and need a neutral, setting-free base — so it also
            gets no scene dressing and no per-render variety.
        pose: The caller's own pose/composition tags, used verbatim instead
            of a candid cue from the pool ("" = no composition cue at all).
        variation_seed: Seed for the per-render choices (scene outfit pick,
            composition, hairstyle, accessories). None derives it from the
            other inputs, so the same render inputs give the same picture plan.
        costume_pinned: ``costume`` is his granted request, so it is never
            re-dressed for the scene. None asks the Her Day cache
            (wardrobe.granted_request).
        setting: The scene brief (app/image_scene.py) — where and when the
            picture happens. Its place, light and weather close the prompt;
            the scene she dresses for, the candid action and his default
            outfit must fit it; the affection tags lose their generic
            "... setting" words; ``time_of_day`` is ignored. Without one, the
            picture's own tags may still say indoors (her POV), and then the
            action and the hour's light stay indoors too.

    Who decides what she wears, highest first: an outfit he asks for in
    ``request`` and she has on > an explicit bath/bed scene (at the intimacy
    gate) > the outfit the scene calls for, if unlocked (app/wardrobe_scene.py)
    > ``costume``.
    """
    parts = [QUALITY_TAGS, KLUKAI_LORA_TRIGGER]

    from . import image_scene

    # Add affection-aware mood tags
    mood_tags = AFFECTION_MOOD_TAGS.get(affection_level, "") if affection_tags else ""
    if mood_tags and setting is not None:
        # "military setting" must not move a picture out of his bedroom.
        mood_tags = ", ".join(
            t for t in mood_tags.split(", ") if not t.endswith(" setting")
        )
    if mood_tags:
        parts.append(mood_tags)

    # Without a brief, the picture's own words may still place it indoors.
    inferred = (
        image_scene.infer_place(f"{scene_tags} {request or ''}")
        if setting is None and affection_tags
        else None
    )
    indoor = (
        setting.indoor
        if setting is not None
        else (inferred.indoor if inferred is not None else None)
    )

    # Scene-aware descriptors — concise mood expression + time/lighting cue,
    # injected BEFORE the scene tags so they colour the moment without bloat.
    expression = MOOD_EXPRESSION_TAGS.get(mood or "")
    if expression:
        parts.append(expression)
    if time_of_day and setting is None:
        if inferred is not None and inferred.indoor and time_of_day in TIME_OF_DAY_TAGS:
            parts.append(image_scene.time_tags(inferred, time_of_day, indoor=True))
        else:
            lighting = TIME_OF_DAY_TAGS.get(time_of_day)
            if lighting:
                parts.append(lighting)

    # Context for outfit matching: use full context (conversation + scene tags)
    outfit_context = f"{context} {scene_tags}" if context else scene_tags
    # What she is wearing today overrides the keyword-matched outfit, except
    # for an explicit bath/bed scene; otherwise fall back to keyword selection.
    from . import wardrobe, wardrobe_scene, wardrobe_variation

    # Live Portrait (affection_tags=False) keeps a fixed, neutral base.
    vary = affection_tags
    seed_parts: list[object] = [
        scene_tags,
        context,
        request,
        costume,
        affection_level,
        mood,
        time_of_day,
        couple,
    ]
    if setting is not None:
        seed_parts.append(setting.key)
    seed = (
        variation_seed
        if variation_seed is not None
        else wardrobe_variation.derive_seed(*seed_parts)
    )
    cat = wardrobe.catalog()
    costume_id = wardrobe.canonical_id(costume, cat) if costume else None
    costume_tags = wardrobe.image_tags(costume_id, cat) if costume_id else None
    # An outfit he asks for in his own words, and that she has on (the request
    # was granted), outranks everything below — the bath/bed rule included.
    named = wardrobe.detect_outfit_request(request, cat) if request else None
    honoured = named is not None and named == costume_id
    explicit = (
        not honoured
        and affection_level >= INTIMATE_MIN_LEVEL
        and _explicit_scene(request if request is not None else outfit_context)
    )
    # Only his words (or, with none, the picture's own scene tags) set the
    # scene — never her replies in ``context`` — and only a scene that can
    # happen where the brief puts the picture.
    if not vary:
        scene = None
    elif setting is not None:
        scene = setting.wardrobe_scene(request)
    else:
        scene = wardrobe_scene.scene_of(request if request is not None else scene_tags)
    if costume_id and costume_tags and not explicit:
        worn = costume_id
        if scene and named is None:
            pinned = (
                costume_pinned
                if costume_pinned is not None
                else wardrobe.granted_request(costume_id)
            )
            if not pinned:
                worn = (
                    wardrobe_scene.scene_outfit(
                        scene,
                        affection_level,
                        current=costume_id,
                        cat=cat,
                        rng=wardrobe_variation.variation_rng(seed, "scene"),
                    )
                    or costume_id
                )
        klukai_outfit = (
            wardrobe_variation.styled_tags(cat[worn], affection_level, seed)
            if vary
            else costume_tags
        )
    else:
        klukai_outfit = _select_outfit(
            outfit_context, _outfit_map_for(affection_level), KLUKAI_DEFAULT_OUTFIT
        )

    # A candid camera/action cue, so pictures are not one posed portrait —
    # never for Live Portrait or an explicit intimate scene, and a caller's own
    # pose always wins.
    if pose is not None:
        composition = pose.strip()
    elif vary and not explicit:
        composition = wardrobe_variation.composition(
            scene,
            couple=couple,
            framing_text=f"{scene_tags} {request or ''}",
            seed=seed,
            indoor=indoor,
            place_actions=setting.place.actions if setting is not None else (),
        )
    else:
        composition = ""

    if couple:
        commander_outfit = _select_outfit(
            outfit_context,
            COMMANDER_OUTFIT_MAP,
            (setting.commander if setting is not None else None)
            or COMMANDER_DEFAULT_OUTFIT,
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

    if composition:
        parts.append(composition)
    if setting is None or scene_tags:
        parts.append(scene_tags)
    if setting is not None:
        # Kept out of ``outfit_context``: "bedroom" or "night" in the setting
        # must never pick an outfit for either of them.
        parts.append(setting.tags())
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
