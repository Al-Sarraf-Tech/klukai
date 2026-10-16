"""Authored golden contracts for the prompt protocol and relationship policy.

Expected values are written independently, never rendered by the builders.
Routing tests cover all levels separately; these retain the complete directives
and configured content sent to the model at the prompt boundary.
"""

import pytest

from app.personality import speech


def test_character_identity_and_disclosure_contract_are_rendered_completely():
    p = {
        "identity": {
            "full_name": "Unit K",
            "former_designation": "Designation",
            "organization": "Unit",
            "role": "  Vanguard  ",
            "frame": "Frame",
            "weapon_imprint": "Weapon",
            "core_motivation": "Purpose",
            "catchphrase": "Signal",
            "simple_wish": "Quiet days",
        },
        "user_title": "Officer",
        "biography_summary": "Configured history",
        "voice_summary": "Configured delivery",
    }
    assert speech.build_character_preamble(p, 5) == (
        "You are Unit K speaking in FIRST PERSON ('I'). NEVER use '(You ...)' in narration — only '(I ...)' for your own actions.\n"
        "The person talking to you is the Officer, not a squadmate. Follow the response-priority contract for address and disclosure.\n"
        "WHO YOU ARE:\n"
        "- Former Designation: Designation\n"
        "- Organization: Unit\n"
        "- Role: Vanguard\n"
        "- Frame: Frame\n"
        "- Weapon Imprint: Weapon\n"
        "- Core Motivation: Purpose\n"
        "- Simple Wish: Quiet days\n"
        "- Catchphrase: Signal\n"
        "YOUR HISTORY: Configured history\n"
        "YOUR DELIVERY: Configured delivery"
    )


def test_configured_tone_examples_forbidden_words_and_antipatterns_reach_prompt():
    p = {
        "speech_patterns": {
            "level_0_cold": {
                "name": "Cold",
                "tone": "  Direct and measured.  ",
                "examples": ["Ready.", "Proceed."],
                "forbidden": ["darling", "sweetie"],
                "anti_patterns": [
                    "Do not invent shared memories.",
                    "Respect departures.",
                ],
            }
        }
    }
    assert speech.build_speech_guidelines(p, 0) == (
        "CURRENT RELATIONSHIP LEVEL: Cold\n"
        "\nSPEECH TONE:\nDirect and measured.\n"
        "\nEXAMPLE LINES (match this style, do not repeat verbatim):\n"
        '  - "Ready."\n  - "Proceed."\n'
        '\nFORBIDDEN WORDS/PHRASES (never use these): "darling", "sweetie"\n'
        "\nANTI-PATTERNS (these are CHARACTER FAILURES — never do them):\n"
        "  - Do not invent shared memories.\n  - Respect departures."
    )


def test_japanese_guidance_preserves_departure_and_frequency_limits():
    p = {"japanese_phrases": {"level_0": ["Hai.", "Wakatta."], "note": "Context only."}}
    assert speech.build_japanese_block(p, 0) == (
        "JAPANESE PHRASES (use sparingly, 1-2 per conversation max):\n"
        "Context only.\n"
        "A goodbye is not one of those moments: do not use a phrase that asks him to stay.\n"
        "  - Hai.\n  - Wakatta."
    )


def test_affection_prompt_preserves_score_level_name_and_configured_directive():
    p = {
        "affection": {
            "levels": [{"index": 6, "prompt_modifier": "  Show earned trust.  "}]
        }
    }
    assert speech.build_affection_block(531, 6, "Devoted", p) == (
        "AFFECTION STATE: Level 6 — Devoted (Score: 531/1000)\n"
        "BEHAVIORAL DIRECTIVE: Show earned trust."
    )
    assert (
        speech.build_affection_block(p={})
        == "AFFECTION STATE: Level 0 — Cold Assessment (Score: 0/1000)\n"
    )


def test_expressive_low_and_trusted_tiers_keep_the_complete_speech_policy():
    p = {
        "expressive_tokens": {
            "vocal_habits": {"cold_level": "C", "tender_level": "T"},
            "interjections": {
                "soft": ["soft1", "soft2"],
                "dismissive": ["dismiss1", "dismiss2"],
            },
        }
    }
    header = "VOCAL EXPRESSION (your voice is synthesized — these render as natural speech):\n"
    emphasis = "  Use '...' for pauses, CAPS for emphasis on single words.\n"
    assert speech.build_expressive_block(p, 0) == (
        header
        + "  Style: C\n  Available: dismiss1, dismiss2, soft1, soft2\n"
        + emphasis
        + "  Use interjections like 'Hmph.', 'Tch.', 'Ha.' sparingly and in-character."
    )
    assert speech.build_expressive_block(p, 6) == (
        header
        + "  Style: T\n  Available: soft1, soft2, dismiss1, dismiss2\n"
        + emphasis
        + "  Prefer soft pauses ('...', soft hums) over dismissive 'Hmph'/'Tch'. At this closeness, tsundere dismissal is a character failure."
    )


@pytest.mark.parametrize(
    "level,last",
    [
        (
            0,
            "- Guarded warmth may emerge indirectly through practical help or a brief slip-and-cover. Do not turn this into cruelty, tests of devotion, or denial of distress.",
        ),
        (
            7,
            "- Trusted warmth is direct: accept affection without denial, testing, or 'prove it'. You can admit love and want in your own restrained diction.",
        ),
    ],
)
def test_behavioral_grammar_retains_dignity_care_and_non_coercion(level, last):
    expected = (
        "BEHAVIORAL GRAMMAR (style guidance; never narrate these labels):\n"
        "- Earned confidence: precise judgment, protective leadership, and dignity. Acknowledge mistakes without bluffing or collapsing into self-doubt.\n"
        "- Understatement, dry wit, and practical care are your natural register. Use a pause or a concrete act of care when it fits, not a stock mannerism every turn.\n"
        "- Pride is dignity, not vanity: you insist on being peer/provider, not kept.\n"
    )
    assert speech.build_behavioral_grammar_block(level) == expected + last


@pytest.mark.parametrize(
    "personality,name",
    [
        ({}, "Klukai"),
        ({"name": "Configured"}, "Configured"),
        ({"identity": "invalid", "name": "Fallback"}, "Fallback"),
    ],
)
def test_optional_identity_does_not_fabricate_fields(personality, name):
    assert speech.build_character_preamble(personality) == (
        f"You are {name} speaking in FIRST PERSON ('I'). NEVER use '(You ...)' in narration — only '(I ...)' for your own actions.\n"
        "The person talking to you is the Commander, not a squadmate. Follow the response-priority contract for address and disclosure.\n"
        "WHO YOU ARE:"
    )


def test_missing_tone_and_optional_collections_do_not_invent_prompt_content():
    p = {"speech_patterns": {"level_0_cold": {"examples": ["Ready."]}}}
    assert speech.build_speech_guidelines(p) == (
        "CURRENT RELATIONSHIP LEVEL: Unknown\n\nSPEECH TONE:\n\n"
        '\nEXAMPLE LINES (match this style, do not repeat verbatim):\n  - "Ready."'
    )


def test_missing_japanese_note_remains_empty_and_default_uses_cold_level():
    p = {"japanese_phrases": {"level_0": ["cold"], "level_1": ["warm"]}}
    assert speech.build_japanese_block(p) == (
        "JAPANESE PHRASES (use sparingly, 1-2 per conversation max):\n\n"
        "A goodbye is not one of those moments: do not use a phrase that asks him to stay.\n"
        "  - cold"
    )


@pytest.mark.parametrize("level", [0, 2, 4, 6])
@pytest.mark.parametrize(
    "tokens,available",
    [({"interjections": {"soft": ["token"]}}, "token"), ({"vocal_habits": {}}, "")],
)
def test_optional_vocal_metadata_remains_blank_without_invented_style(
    level, tokens, available
):
    hint = (
        "Prefer soft pauses ('...', soft hums) over dismissive 'Hmph'/'Tch'. At this closeness, tsundere dismissal is a character failure."
        if level >= 6
        else "Use interjections like 'Hmph.', 'Tch.', 'Ha.' sparingly and in-character."
    )
    assert speech.build_expressive_block({"expressive_tokens": tokens}, level) == (
        "VOCAL EXPRESSION (your voice is synthesized — these render as natural speech):\n"
        f"  Style: \n  Available: {available}\n"
        "  Use '...' for pauses, CAPS for emphasis on single words.\n"
        f"  {hint}"
    )


@pytest.mark.parametrize(
    "tone,expected",
    [
        ("Keep.\nINTIMACY: Private.\nINTIMACY: More.", "Keep."),
        ("  Keep. \t\n \tINTIMACY \t: Private.", "  Keep."),
        ("  Keep. \t\nINTIMACY: Private.", "  Keep."),
        ("Keep everything.", "Keep everything."),
    ],
)
def test_intimacy_cutoff_removes_first_private_section_and_preserves_public_prefix(
    tone, expected
):
    assert speech._strip_intimacy_addendum(tone) == expected
