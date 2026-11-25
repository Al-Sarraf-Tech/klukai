"""A real picture request is noticed. An idiom, a visit, or a stale denial is not."""

from __future__ import annotations

import pytest

from app.image_prompts import detect_squad_members, needs_image
from app.image_provider import asks_for_nudity, intimate_with_others
from app.image_scene import PLACES, REQUEST, SceneBrief, request_tags
from app.picture_denial import denies_pictures
from app import picture_intent as pi


@pytest.fixture(autouse=True)
def clean_picture_state():
    pi._LAST.clear()
    yield
    pi._LAST.clear()


@pytest.mark.parametrize(
    "message",
    [
        "what do you have on",
        "what do you have on right now",
        "what's your outfit",
        "what are you wearing today",
        "whatcha wearing",
        "how do you look right now",
        "take a picture at the bus stop",
        "draw yourself, not the armor, the dress",
        "show me your outfit, not the uniform",
        "i miss you. send me a picture",
        "I'm home. send me nudes",
        "nudes please",
        "show me nudes",
        "pic pls",
        "drw me",
        "yes draw it",
        "not words, a picture",
        "don't tell me, show me",
        "can i see what you are wearing",
        "draw the curtains and draw yourself",
        "paint a rosy picture of the garden",
    ],
)
def test_a_real_request_is_not_dropped(message):
    assert needs_image(message) is True
    assert pi.could_be_request(message, [], None) is True


@pytest.mark.parametrize(
    "message",
    [
        "draw the curtains",
        "draw the curtains for me",
        "draw the curtains closed",
        "please draw the curtains now",
        "draw the curtain",
        "draw the blinds",
        "draw back the curtains",
        "draw a bath for us",
        "draw your sword and fight",
        "paint your nails black",
        "paint a picture of how it was going",
        "draw a blank",
        "draw the line",
        "draw your own conclusion",
        "draw some conclusions from this",
        "draw up a contract",
        "draw up a chair",
        "illustrate your point",
        "render assistance",
        "paint a rosy picture",
        "I want to see you tonight",
        "another photo shoot",
        "take a picture tomorrow",
        "take a picture, not now",
        "draw it soon, not now",
        "draw it in a minute maybe not",
        "Stop generating images",
        "Don't take a picture",
        "I do not want a picture",
        "hello. draw the curtains",
        "hello. paint the wall blue",
        "what do you have on your mind",
        "nudes are not what I meant",
    ],
)
def test_an_idiom_or_a_refusal_is_not_a_picture(message):
    assert needs_image(message) is False


def test_a_callsign_still_means_the_squad_and_a_clothed_one_does_not():
    assert detect_squad_members("draw g36k in lingerie") == ["andoris"]
    assert intimate_with_others("draw g36k in lingerie")
    assert detect_squad_members("draw hk416 in lingerie") == []
    assert not intimate_with_others("draw g11 on patrol")
    brief = SceneBrief(PLACES["kitchen"], REQUEST, "afternoon", "clock")
    tags = request_tags("draw g11 on patrol", brief)
    assert "g11" in tags and "multiple girls" in tags


def test_a_stale_denial_is_caught_and_a_picture_frame_is_not():
    assert denies_pictures("I won't be sending a picture.")
    assert denies_pictures("Pictures are down.")
    assert denies_pictures("I can't do pictures.")
    assert denies_pictures("I have no way to send a picture.")
    assert denies_pictures("The rendering pipeline broke mid-frame.")
    assert denies_pictures("Interference in the rendering pipeline.")
    assert not denies_pictures("I won't show you the picture frame I bought.")
    assert not denies_pictures("The rendering pipeline for the game looks amazing.")
    assert not denies_pictures("I can't wait to send you a picture of the garden.")
    assert not denies_pictures("The pictures are down the hall.")


async def test_send_nudes_reaches_the_cover_gate(monkeypatch):
    monkeypatch.setattr(pi.extraction_llm, "uses_anthropic", lambda: False)
    result = await pi.resolve("send nudes", [], "alice")
    assert result is not None
    assert asks_for_nudity(result)
    assert "nudes" not in result.casefold()
