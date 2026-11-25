"""Regressions for on-demand pictures: ask, draw, don't refuse, don't blank."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.image_prompts import detect_squad_members, needs_image
from app.image_provider import intimate_with_others
from app.picture_denial import denies_pictures
from app import picture_intent as pi
from app.picture_tap import SCENE_TURN_CHARS, _scene_excerpt

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from grok_image_check import background_only_passes  # noqa: E402
import xai_image  # noqa: E402


@pytest.fixture(autouse=True)
def clean_picture_state():
    pi._LAST.clear()
    yield
    pi._LAST.clear()


@pytest.mark.parametrize(
    "message",
    [
        "what are you wearing right now",
        "what do you look like right now",
        "send nudes",
        "send me nudes",
        "klukai, please send nudes",
    ],
)
def test_a_real_picture_request_is_admitted(message):
    assert needs_image(message) is True
    assert pi.could_be_request(message, [], None) is True


@pytest.mark.parametrize(
    "message",
    [
        "draw the curtains",
        "draw a bath",
        "draw your sword",
        "paint your nails",
        "paint a picture of how it was",
        "take a picture tomorrow",
        "I saw a picture of a dog",
    ],
)
def test_a_mention_or_idiom_is_not_a_picture(message):
    assert needs_image(message) is False


async def test_a_bath_does_not_let_the_subject_add_naked(monkeypatch):
    async def complete(prompt, **kwargs):
        return '{"draw": true, "subject": "her naked", "again": false}'

    monkeypatch.setattr(pi.extraction_llm, "uses_anthropic", lambda: True)
    monkeypatch.setattr(pi.extraction_llm, "complete_claude", complete)
    result = await pi.resolve("draw yourself in the bath", [], "alice")
    assert result == "draw yourself in the bath"
    assert "naked" not in result


async def test_his_own_naked_still_carries(monkeypatch):
    async def complete(prompt, **kwargs):
        return '{"draw": true, "subject": "her nude", "again": false}'

    monkeypatch.setattr(pi.extraction_llm, "uses_anthropic", lambda: True)
    monkeypatch.setattr(pi.extraction_llm, "complete_claude", complete)
    result = await pi.resolve("draw yourself naked", [], "alice")
    assert result is not None and "Subject: her nude" in result


async def test_a_retry_after_failure_says_she_is_taking_it_not_that_it_failed(monkeypatch):
    async def complete(prompt, **kwargs):
        return '{"draw": true, "subject": "", "again": true}'

    monkeypatch.setattr(pi.extraction_llm, "uses_anthropic", lambda: True)
    monkeypatch.setattr(pi.extraction_llm, "complete_claude", complete)
    pi.note_picture("alice", "failed", "draw a red fox")
    await pi.resolve("try again", [], "alice")
    assert pi.last_picture("alice")["outcome"] == "drawing"
    note = pi.state_note("alice")
    assert "still being drawn" in note
    assert "failed" not in note


@pytest.mark.parametrize(
    "text",
    [
        "I won't send you a picture.",
        "I refuse to take a picture.",
        "...The rendering pipeline broke mid-frame, Commander. Ask me again.",
        "Visualization failed. Interference in the rendering pipeline.",
    ],
)
def test_a_failure_line_is_a_denial_she_must_not_repeat(text):
    assert denies_pictures(text)


def test_waiting_to_send_a_picture_is_not_a_refusal():
    assert not denies_pictures("I can't wait to send you a picture of the garden.")


@pytest.mark.parametrize(
    "message",
    ["draw g11 in lingerie", "draw ump45 kissing you", "you and ots-14 in bed"],
)
def test_a_callsign_beside_intimacy_is_the_squad(message):
    assert detect_squad_members(message)
    assert intimate_with_others(message)


def test_elevator_and_a_clothed_callsign_still_pass():
    assert detect_squad_members("draw Klukai in the elevator") == []
    assert not intimate_with_others("draw g11 on patrol")


def test_a_tap_keeps_the_place_the_turn_ends_on():
    from app.image_scene import resolve_scene

    opening = "We are at the beach. "
    ending = "I'm in the hangar now."
    text = opening + ("hm " * 400) + ending
    assert "hangar" not in text[:SCENE_TURN_CHARS]
    excerpt = _scene_excerpt(text)
    assert "hangar" in excerpt
    brief = resolve_scene(
        "take a picture",
        [{"role": "assistant", "content": excerpt}],
        hour=15,
    )
    assert brief.key == "hangar"


def test_a_differing_background_does_not_reject_the_picture():
    verdict = background_only_passes(
        {
            "verdict": "reject",
            "issues": [
                {
                    "severity": "major",
                    "what": "the background is a street instead of the room",
                }
            ],
        }
    )
    assert verdict["verdict"] == "pass"
    assert verdict["issues"][0]["severity"] == "minor"
    # The live checker and the CLI checker share this rule.
    assert xai_image.background_only_passes is background_only_passes


def test_a_photoreal_background_is_still_a_reject():
    verdict = background_only_passes(
        {
            "verdict": "reject",
            "issues": [{"severity": "major", "what": "photorealistic background"}],
        }
    )
    assert verdict["verdict"] == "reject"
    assert verdict["issues"][0]["severity"] == "major"


def test_a_frame_signature_outlives_the_image_cache():
    from app.portrait_assets import _sign_ttl
    from app.portrait_style import SIGN_BUCKET_S
    from app.routes_portrait import FRAME_CACHE

    assert "max-age=1800" in FRAME_CACHE
    # One second before the bucket rolls: the shortest signature this bucket issues.
    ttl = _sign_ttl(SIGN_BUCKET_S * 5 + SIGN_BUCKET_S - 1)
    assert ttl > 1800
