"""Weather → mood mapping + in-character weather line for Klukai.

Maps the coarse condition from ``weather_client.fetch_weather`` onto a valid
taxonomy mood (a key of ``app.personality.moods.MOOD_SPECIFIC_BLEED``) so the
morning greeting / proactive scheduler can shade her bearing by the weather
where the Commander actually is. Fully fail-soft: ``None`` weather yields no
mood change and an empty phrase.

Affection shades the warm-weather read (the more she's let her guard down, the
warmer clear skies make her) but the mapping stays deliberately simple.
"""

from __future__ import annotations

# Condition → mood. Every value MUST be a key of moods.MOOD_SPECIFIC_BLEED.
# Affection only shades the "clear" bucket (see weather_to_mood).
_CONDITION_MOOD: dict[str, str] = {
    "clear": "playful",       # sunshine — light, teasing (warmed further by affection)
    "cloudy": "composed",     # grey/overcast — measured, professional
    "fog": "composed",        # low visibility — steady, watchful composure
    "drizzle": "tender",      # soft rain — guard lowers a little
    "rain": "tender",         # steady rain — quiet, close, tender
    "snow": "content",        # snow — cozy, settled
    "storm": "protective",    # thunderstorm — wants him safe indoors
}

# Fallback when a condition isn't recognized: neutral, always-valid.
_DEFAULT_MOOD = "composed"

# Condition → short in-character line (caring, slightly military GFL T-Doll).
# One sentence each; no newlines.
_CONDITION_PHRASE: dict[str, str] = {
    "clear": "Clear skies over your position, Commander — good day to be out, if duty allows.",
    "cloudy": "Overcast where you are — keep your bearing, the grey always lifts eventually.",
    "fog": "Visibility's low on your end — watch your step out there, yeah?",
    "drizzle": "Light rain on your position — grab a jacket before you head out, Commander.",
    "rain": "It's raining where you are — stay dry, and don't push yourself too hard today.",
    "snow": "Snow's coming down on your end — bundle up, and mind the ice when you move.",
    "storm": "It's storming where you are — stay in tonight, yeah? I'd rather you safe.",
}

_DEFAULT_PHRASE = "Weather's shifting where you are — stay sharp out there, Commander."


def weather_to_mood(weather: dict | None, affection_level: int) -> str | None:
    """Map current weather to a valid taxonomy mood.

    Args:
        weather: Normalized dict from ``weather_client.fetch_weather`` (or None).
        affection_level: 0-9; shades the warm-weather read only.

    Returns:
        A mood string guaranteed to be a key of ``MOOD_SPECIFIC_BLEED``, or
        ``None`` when ``weather`` is None (caller keeps the current mood).
    """
    if weather is None:
        return None

    condition = weather.get("condition", "")

    # Clear skies warm with affection: the more her guard is down, the fonder
    # the sun makes her. All three values are valid taxonomy moods.
    if condition == "clear":
        if affection_level >= 7:
            return "affectionate"
        if affection_level >= 3:
            return "content"
        return "playful"

    return _CONDITION_MOOD.get(condition, _DEFAULT_MOOD)


def weather_phrase(weather: dict | None) -> str:
    """Return a short, single-sentence in-character line about the weather.

    Returns ``""`` when ``weather`` is None.
    """
    if weather is None:
        return ""
    condition = weather.get("condition", "")
    return _CONDITION_PHRASE.get(condition, _DEFAULT_PHRASE)
