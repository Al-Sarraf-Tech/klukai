"""Deterministic prompt wiring checks; these do not evaluate generated dialogue."""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from app.personality.speech import build_character_preamble
from app.personality.state_blocks import build_quirks_block
from app.personality.system_prompt import assemble_system_prompt
from app.personality.thread import build_thread_block

CONFIG = Path(__file__).resolve().parents[3] / "config/personality.yaml"


@pytest.mark.parametrize("path", [
    ("identity", "core_motivation"), ("biography_summary",),
    ("canonical_arcs", "aphelion", "climax"),
])
def test_supported_character_edits_reach_assembly(tmp_path, path):
    config = yaml.safe_load(CONFIG.read_text())
    changed = deepcopy(config)
    node = changed
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = "Distinctive revised character fact 4.5... H.I.D.E. 404."
    edited_path = tmp_path / "personality.yaml"
    edited_path.write_text(yaml.safe_dump(changed))
    prompt = assemble_system_prompt(personality_path=str(edited_path), affection_level=9)
    assert "Distinctive revised character fact 4.5... H.I.D.E. 404." in prompt
    original = config
    for key in path:
        original = original.get(key, "")
    if original:
        assert original.strip() not in prompt


def test_complete_settlement_and_cat_ear_discretion_reach_assembly():
    prompt = assemble_system_prompt(personality_path=str(CONFIG), affection_level=3)
    assert "H.I.D.E. 404 maintains independent fee settlements" in prompt
    assert "Never introduce the cat ears unprompted" in prompt


def test_explicit_quirk_summary_preserves_punctuation_and_disclosure():
    config = {"character_quirks": [
        {"label": "precision", "prompt_summary": "H.I.D.E. 404: 4.5... complete.",
         "disclosure": "Only when asked.", "min_affection": 3},
        {"label": "empty"},
    ]}
    assert build_quirks_block(config, 2) == ""
    block = build_quirks_block(config, 3)
    assert "H.I.D.E. 404: 4.5... complete. Only when asked." in block
    assert "empty" not in block


@pytest.mark.parametrize("level", [0, 5, 9])
def test_brief_input_and_departure_have_no_sentence_floor(level):
    prompt = assemble_system_prompt(personality_path=str(CONFIG), affection_level=level,
                                    last_msg_length=2, jealousy_target="Groza")
    assert "RESPONSE PRIORITY" in prompt
    assert "no minimum sentence count" in prompt
    assert "2-4 sentences minimum" not in prompt
    assert "4-8 sentences minimum" not in prompt
    assert "demand attention" not in prompt
    assert "self-deprecating withdrawal" not in prompt


def test_the_persona_prefix_is_identical_when_only_the_turn_changes():
    calm = assemble_system_prompt(
        personality_path=str(CONFIG),
        affection_level=5,
        affection_score=10,
        mood="composed",
        memories=["the harbour"],
        last_msg_length=12,
        days_together=3,
        current_outfit="tactical",
        mission_description=None,
    )
    later = assemble_system_prompt(
        personality_path=str(CONFIG),
        affection_level=5,
        affection_score=400,
        mood="tender",
        memories=["a different night"],
        last_msg_length=420,
        days_together=90,
        current_outfit="dorm casual",
        current_location="the rec room",
        mission_description="east patrol",
        physical_state="tired",
        physical_detail="a long watch",
        jealousy_target="Groza",
        addressed_member="Vector",
    )
    assert calm.stable == later.stable
    assert calm.stable
    assert calm.startswith(calm.stable)
    assert "OPERATIONAL CONTEXT" not in calm.stable
    assert "PACE MATCHING" not in calm.stable
    assert "the harbour" not in calm.stable
    assert "OPERATIONAL CONTEXT" in calm
    assert calm.stable in later


def test_answer_discipline_closes_the_prompt_and_gates_the_name():
    low = assemble_system_prompt(personality_path=str(CONFIG), affection_level=0)
    high = assemble_system_prompt(personality_path=str(CONFIG), affection_level=9,
                                  tools_available=True)
    assert low.rstrip().endswith(
        "Below level 7, address him only as Commander, even if a name is visible "
        "and even in a close moment."
    )
    assert "accept love in one or two short sentences, then stop" in high
    assert "Do not name a sector, a direction, or a path" in high
    assert high.rstrip().endswith("A routine briefing stays Commander.")
    assert "do not use a phrase that asks him to stay" in high


def test_low_affection_keeps_recognition_and_one_address_policy():
    prompt = assemble_system_prompt(personality_path=str(CONFIG), affection_level=0)
    assert "You recognize the established Commander at every affection level" in prompt
    assert "Trust is unearned" not in prompt
    assert "NEVER by any other name" not in prompt
    assert prompt.count("ADDRESS POLICY:") == 1
    assert "At level 7+" in prompt and "his known name" in prompt


@pytest.mark.parametrize("mood", ["composed", "competitive", "jealous", "infatuated"])
def test_trusted_warmth_has_priority_over_mood(mood):
    prompt = assemble_system_prompt(personality_path=str(CONFIG), affection_level=9,
                                    mood=mood, jealousy_target="Groza")
    assert "Mood is delivery, not permission to override trust or context" in prompt
    assert "No emotional leakage" not in prompt
    assert "The denial IS the character" not in prompt
    assert "Pet names slip through" not in prompt
    assert "Repeats his name" not in prompt
    assert "You may occasionally drop the title" not in prompt


def test_thread_entries_identified_as_authored_and_gated():
    config = yaml.safe_load(CONFIG.read_text())
    sealed = build_thread_block(config, 0)
    opened = build_thread_block(config, 9)
    assert "authored project continuity" in opened
    assert "not verbatim game records" in opened
    assert "Every night at 0200" not in opened
    assert config["ten_year_thread"]["entries"][0]["text"] not in sealed
    assert config["ten_year_thread"]["entries"][0]["text"] in opened


def test_minimal_preamble_has_small_explicit_identity_fallback():
    prompt = build_character_preamble({"user_title": "Captain"})
    assert "Klukai" in prompt and "Captain" in prompt
    assert "NSA6" not in prompt and "Slovakia" not in prompt


def test_invalid_optional_lore_is_omitted_without_crashing():
    from app.personality.state_blocks import build_canon_arcs_block

    assert build_canon_arcs_block({"canonical_arcs": None}, 9) == ""
    assert "WHO YOU ARE" in build_character_preamble({"identity": None, "biography_summary": []})


def test_null_quirk_collection_is_omitted_without_crashing():
    assert build_quirks_block({"character_quirks": None}, 9) == ""


def test_identity_confidence_does_not_require_unverified_claims():
    prompt = assemble_system_prompt(personality_path=str(CONFIG), affection_level=9)
    assert "Never deny knowledge of things" not in prompt
    assert "Distinguish private information from unverified information" in prompt


@pytest.mark.parametrize("level", [0, 9])
@pytest.mark.parametrize("replacement", ["Stand steady, Commander.", None])
def test_catchphrase_edit_has_no_competing_absolute_literal(tmp_path, level, replacement):
    config = yaml.safe_load(CONFIG.read_text())
    if replacement is None:
        config["identity"].pop("catchphrase")
    else:
        config["identity"]["catchphrase"] = replacement
    edited = tmp_path / "personality.yaml"
    edited.write_text(yaml.safe_dump(config))
    prompt = assemble_system_prompt(personality_path=str(edited), affection_level=level)
    assert 'Your catchphrase is "I am all you need."' not in prompt
    if replacement is not None:
        assert f"Catchphrase: {replacement}" in prompt
    else:
        assert "- Catchphrase:" not in prompt
