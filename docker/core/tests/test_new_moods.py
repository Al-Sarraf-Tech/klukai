"""The fifteen 2026-10-19 moods reach every mood-keyed behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

NEW_MOODS = (
    "serene",
    "cozy",
    "wistful",
    "lonely",
    "stern",
    "smug",
    "hopeful",
    "sulking",
    "admiring",
    "teasing",
    "restless",
    "heartbroken",
    "giddy",
    "overwhelmed",
    "breathless",
)

# Moods that read as turned AGAINST him. When he is hurting she must land
# on none of these (feedback: distress is not hostility).
AT_HIM = {
    "irritated",
    "exasperated",
    "defiant",
    "furious",
    "sulking",
    "stern",
    "smug",
    "teasing",
    "heartbroken",
}


@pytest.mark.parametrize("mood", NEW_MOODS)
def test_new_mood_is_known_everywhere(mood):
    from app.fact_extractor import VALID_MOODS
    from app.models import Mood
    from app.personality.moods import MOOD_CATEGORIES, MOOD_SPECIFIC_BLEED

    assert Mood(mood).value == mood
    assert mood in VALID_MOODS
    assert mood in MOOD_CATEGORIES
    assert mood in MOOD_SPECIFIC_BLEED


@pytest.mark.parametrize("mood", NEW_MOODS)
def test_new_mood_prompt_block_is_its_own_not_the_default(mood):
    from app.personality.moods import build_mood_bleed_block

    block = build_mood_bleed_block(mood)
    assert f"MOOD COLORING — {mood.upper()}:" in block


def test_categories_fit_the_mood():
    from app.personality.moods import MOOD_CATEGORIES

    assert MOOD_CATEGORIES["stern"] == "core"
    assert MOOD_CATEGORIES["overwhelmed"] == "stress"
    assert MOOD_CATEGORIES["heartbroken"] == "dark"
    assert MOOD_CATEGORIES["teasing"] == "casual"


@pytest.mark.parametrize(
    "mood", sorted(__import__("app.models", fromlist=["Mood"]).Mood.__members__)
)
def test_distress_never_leaves_her_turned_against_him(mood):
    from app.character_behaviors import interaction_to_sentiment, nudge_mood
    from app.fact_extractor import VALID_MOODS

    sentiment = interaction_to_sentiment({"type": "hurt", "intensity": 9})
    landed = nudge_mood(mood, sentiment)
    assert landed in VALID_MOODS
    assert landed not in AT_HIM, f"{mood} -> {landed} while he is hurting"


@pytest.mark.parametrize(
    ("mood", "expected"),
    [
        ("sulking", "worried"),
        ("stern", "protective"),
        ("heartbroken", "protective"),
        ("teasing", "tender"),
        ("smug", "tender"),
        ("furious", "protective"),
    ],
)
def test_distress_turns_new_moods_toward_him(mood, expected):
    from app.character_behaviors import nudge_mood

    assert nudge_mood(mood, "distress") == expected


def test_every_nudge_target_is_a_real_mood():
    from app.character_behaviors import _MOOD_NUDGES
    from app.fact_extractor import VALID_MOODS

    for sentiment, table in _MOOD_NUDGES.items():
        assert set(table.values()) <= VALID_MOODS, sentiment


def test_hostility_cools_the_light_new_moods():
    from app.character_behaviors import nudge_mood

    for mood in ("teasing", "giddy", "cozy"):
        assert nudge_mood(mood, "negative_heavy") == "composed"


def test_warmth_coaxes_her_out_of_a_sulk():
    from app.character_behaviors import nudge_mood

    assert nudge_mood("sulking", "positive") == "quietly_pleased"
    assert nudge_mood("lonely", "positive") == "content"


def test_weather_uses_the_new_moods():
    from app.weather_mood import weather_to_mood

    assert weather_to_mood({"condition": "snow"}, 5) == "cozy"
    assert weather_to_mood({"condition": "drizzle"}, 5) == "wistful"


@pytest.mark.parametrize("mood", NEW_MOODS)
def test_image_prompts_have_an_expression_for_each_new_mood(mood):
    from app.image_gen_constants import MOOD_EXPRESSION_TAGS

    assert MOOD_EXPRESSION_TAGS[mood]


def test_memory_recall_has_affinities_for_the_new_soft_moods():
    from app.memory_archive import MOOD_CATEGORY_WEIGHTS

    for mood in ("wistful", "lonely", "cozy", "admiring", "heartbroken"):
        assert MOOD_CATEGORY_WEIGHTS[mood]


def _personality():
    path = Path(__file__).resolve().parents[3] / "config" / "personality.yaml"
    return yaml.safe_load(path.read_text())


def test_personality_emotional_range_lists_every_mood():
    from app.models import Mood

    listed = set(_personality()["emotional_range"]["can_express"])
    assert set(NEW_MOODS) <= listed
    assert listed <= {m.value for m in Mood}


def test_wardrobe_moods_are_valid_and_use_new_moods():
    from app.models import Mood

    valid = {m.value for m in Mood}
    used: set[str] = set()
    for costume_id, costume in _personality()["costumes"].items():
        moods = (costume.get("daily") or {}).get("moods") or []
        assert set(moods) <= valid, costume_id
        used.update(moods)
    assert {"cozy", "serene", "teasing", "wistful", "giddy", "stern"} <= used
