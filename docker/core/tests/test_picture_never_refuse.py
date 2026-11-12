"""A picture request is never refused: she says she is taking it and it is drawn."""

from __future__ import annotations

import pytest

from app.chat_prompt import PICTURE_NOT_UNDERWAY, PICTURE_UNDERWAY
from app.image_prompts import needs_image as explicit_picture_request
from app.picture_denial import NEUTRAL, denies_pictures, neutralise_denials

DENIALS = [
    "Still no picture, Al-Sarraf. No image tool is working for me, so nothing would reach you.",
    "I can't send you a picture right now.",
    "I cannot take a photo of that.",
    "The image tool is down, sorry.",
    "No picture is coming tonight.",
    "I’m unable to generate an image at the moment.",
]
HARMLESS = [
    "I can't wait to send you a picture of the garden when it blooms.",
    "The picture on the wall is crooked.",
    "I took a photo of the sunset yesterday, remember?",
    "Taking it now. Hold on.",
]


@pytest.mark.parametrize("text", DENIALS)
def test_denials_are_detected(text):
    assert denies_pictures(text)


@pytest.mark.parametrize("text", HARMLESS)
def test_ordinary_talk_is_not_a_denial(text):
    assert not denies_pictures(text)


def test_neutralise_only_touches_her_denials_and_copies():
    turns = [
        {"role": "user", "content": "I can't send you a picture"},
        {"role": "assistant", "content": DENIALS[0]},
        {"role": "assistant", "content": HARMLESS[0]},
    ]
    out = neutralise_denials(turns)
    assert out[0] == turns[0]
    assert out[1] == {"role": "assistant", "content": NEUTRAL}
    assert out[2] == turns[2]
    assert turns[1]["content"] == DENIALS[0]  # stored history untouched


@pytest.mark.parametrize(
    "text",
    [
        "Take a picture for me — show me where you are.",
        "show me where you are",
        "Show me what you're doing right now",
        "let me see where you are",
        "where are you? show me where you are",
        "klukai, show me what you are wearing",
    ],
)
def test_where_you_are_requests_admit_a_picture(text):
    assert explicit_picture_request(text)


@pytest.mark.parametrize("text", ["show me the code", "show me", "show me later"])
def test_loose_show_me_is_not_admitted(text):
    assert not explicit_picture_request(text)


def test_underway_hint_commits_to_sending_and_forbids_refusal():
    for need in ("taking it for him now", "Never refuse", "ignore any earlier turn"):
        assert need in PICTURE_UNDERWAY
    assert "without a successful tool result" not in PICTURE_UNDERWAY
    assert "has not arrived yet" in PICTURE_UNDERWAY
    assert "fails" in PICTURE_UNDERWAY and "after the attempt" in PICTURE_UNDERWAY


def test_idle_hint_states_the_capability():
    assert "You can draw and send him pictures" in PICTURE_NOT_UNDERWAY
    assert "never say one is" in PICTURE_NOT_UNDERWAY
    assert "no image tool" in PICTURE_NOT_UNDERWAY


async def test_stale_no_picture_turn_never_reaches_the_reply_when_a_picture_is_drawn():
    from unittest.mock import patch

    from app.chat_handlers import _handle_message
    from app.image_prompts import needs_image
    from tests.test_chat_handlers_coverage import (
        _drain_tasks,
        _fresh_session,
        _patched_pipeline,
    )

    denial = (
        "Still no picture. No image tool is working for me, so nothing would reach you."
    )
    session = _fresh_session(
        turns=[
            {"role": "user", "content": "send me a pic"},
            {"role": "assistant", "content": denial},
        ]
    )
    captured = {}

    async def stream(system_prompt, messages, config):
        captured["prompt"] = system_prompt
        captured["messages"] = messages
        yield "Taking it now."

    with _patched_pipeline() as ns:
        ns.router.stream = stream
        with patch("app.chat_handlers.needs_image", needs_image):
            await _handle_message(
                "Take a picture for me — show me where you are.", session
            )
            await _drain_tasks()
    ns.background_image_gen.assert_awaited_once()
    assert "Never refuse" in captured["prompt"]
    joined = " ".join(str(m.get("content")) for m in captured["messages"])
    assert "No image tool" not in joined and "Still no picture" not in joined


def test_the_still_no_picture_running_joke_is_a_refusal_reference():
    from app.picture_denial import (
        refusal_reference,
        without_denied_exchanges,
        without_refusal_references,
    )

    note = "She playfully deflects his repeated requests for a photo of where she is."
    assert refusal_reference("Still no picture", note)
    assert refusal_reference("the photo bit", note)
    assert not refusal_reference("Garden roses", "She grows roses on the balcony.")
    jokes = [
        {"label": "Still no picture", "note": note},
        {"label": "Garden roses", "note": "balcony"},
    ]
    assert without_refusal_references(jokes) == [jokes[1]]
    rows = [
        {"assistant_content": "Still no picture, sorry."},
        {"assistant_content": "Roses are in bloom."},
    ]
    assert without_denied_exchanges(rows) == [rows[1]]
