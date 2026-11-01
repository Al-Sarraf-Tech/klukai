"""She draws when he asks, in any wording, and only then.

Keyword rules caught 155 of 444 realistic requests and fired on 49 of 94 ordinary
sentences. Claude Haiku now reads the message in context. Keyword rules remain the
fallback when Haiku is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import patch

import pytest

from app import extraction_llm, picture_intent as pi


@pytest.fixture(autouse=True)
def clean_state():
    pi._LAST.clear()
    yield
    pi._LAST.clear()


@pytest.fixture
def haiku(monkeypatch):
    """Haiku answering with a fixed verdict; records what it was sent."""
    calls = []
    answer = {"text": '{"draw": true, "subject": "a red fox", "again": false}'}

    async def complete(prompt, **kwargs):
        calls.append((prompt, kwargs))
        if isinstance(answer["text"], Exception):
            raise answer["text"]
        return answer["text"]

    monkeypatch.setattr(extraction_llm, "uses_anthropic", lambda: True)
    monkeypatch.setattr(extraction_llm, "complete_claude", complete)
    return answer, calls


def turns(*pairs):
    return [{"role": role, "content": text} for role, text in pairs]


@pytest.mark.parametrize(
    "message",
    [
        "draw it",
        "yes draw it",
        "i miss you. send me a picture",
        "pic pls",
        "drw me",
        "doodle something",
        "what do you look like right now",
        "try again",
        "another one",
        "make one",
        "send me one",
        "let's see it",
    ],
)
def test_anything_that_could_be_a_request_gets_read(message):
    assert pi.could_be_request(message, [], None)


@pytest.mark.parametrize(
    "message",
    [
        "good morning",
        "how was your day",
        "I'm tired, the meeting ran long",
        "yes",
        "do it",
        "go ahead",
    ],
)
def test_ordinary_chat_costs_no_extra_call(message):
    assert not pi.could_be_request(message, [], None)


@pytest.mark.parametrize("message", ["yes", "ok do it", "go ahead", "please", "sure"])
def test_a_short_yes_counts_right_after_she_offers(message):
    offered = turns(("user", "that sounds nice"), ("assistant", "Want me to sketch it?"))
    assert pi.could_be_request(message, offered, None)


def test_a_short_reply_counts_right_after_a_picture():
    assert pi.could_be_request("better", [], {"outcome": "failed", "request": "x"})


def test_a_long_reply_after_her_offer_needs_its_own_cue():
    offered = turns(("assistant", "Want me to sketch it?"))
    message = "yes the meeting really did run long and I am exhausted today"
    assert not pi.could_be_request(message, offered, None)


async def test_his_request_comes_back_with_its_subject(haiku):
    assert await pi.resolve("draw it", turns(("user", "a red fox")), "alice") == (
        "draw it\n\nSubject: a red fox"
    )


async def test_it_reads_the_chat_and_his_last_picture(haiku):
    answer, calls = haiku
    pi.note_picture("alice", "failed", "draw a red fox")
    await pi.resolve("try again", turns(("user", "hi"), ("assistant", "hello")), "alice")
    prompt = calls[0][0]
    assert "try again" in prompt and "hello" in prompt
    assert "failed" in prompt and "draw a red fox" in prompt
    assert calls[0][1]["route"] == "picture_intent"
    assert calls[0][1]["system"] == pi.SYSTEM


async def test_no_subject_is_still_a_request(haiku):
    answer, _ = haiku
    answer["text"] = '{"draw": true, "subject": "", "again": false}'
    assert await pi.resolve("draw something", [], "alice") == "draw something"


async def test_a_malformed_subject_is_ignored(haiku):
    answer, _ = haiku
    answer["text"] = '{"draw": true, "subject": 7, "again": false}'
    assert await pi.resolve("draw something", [], "alice") == "draw something"


async def test_haiku_overrules_the_keywords_on_a_false_alarm(haiku):
    answer, _ = haiku
    answer["text"] = '{"draw": false, "subject": "", "again": false}'
    assert await pi.resolve("draw the curtains", [], "alice") is None


async def test_haiku_catches_what_the_keywords_miss(haiku):
    assert await pi.resolve("i miss you. send me a picture", [], "alice")


async def test_ordinary_chat_never_calls_haiku(haiku):
    _, calls = haiku
    assert await pi.resolve("good morning", [], "alice") is None
    assert calls == []


@pytest.mark.parametrize(
    "broken",
    ["not json", "[]", '{"subject": "x"}', '{"draw": "yes"}', ""],
)
async def test_an_unusable_answer_falls_back_to_the_keywords(haiku, broken):
    answer, _ = haiku
    answer["text"] = broken
    assert await pi.resolve("draw me a cat", [], "alice") == "draw me a cat"
    assert await pi.resolve("draw it in a minute maybe not", [], "alice") is None


@pytest.mark.parametrize(
    "error",
    [
        extraction_llm.ExtractionUnavailable("capped"),
        extraction_llm.ExtractionLLMError("down"),
        RuntimeError("boom"),
    ],
)
async def test_haiku_down_falls_back_to_the_keywords(haiku, error, caplog):
    answer, _ = haiku
    answer["text"] = error
    with caplog.at_level(logging.INFO, logger="app.picture_intent"):
        assert await pi.resolve("draw me a cat", [], "alice") == "draw me a cat"
    assert "picture intent" in caplog.text.lower()
    assert "draw me a cat" not in caplog.text


async def test_a_slow_answer_falls_back_without_holding_up_her_reply(monkeypatch):
    async def stuck(prompt, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(extraction_llm, "uses_anthropic", lambda: True)
    monkeypatch.setattr(extraction_llm, "complete_claude", stuck)
    monkeypatch.setattr(pi, "DECISION_TIMEOUT_S", 0.01)
    assert await pi.resolve("draw me a cat", [], "alice") == "draw me a cat"


async def test_without_claude_the_keywords_decide(monkeypatch):
    async def explode(prompt, **kwargs):
        raise AssertionError("no paid call without the Claude settings")

    monkeypatch.setattr(extraction_llm, "uses_anthropic", lambda: False)
    monkeypatch.setattr(extraction_llm, "complete_claude", explode)
    assert await pi.resolve("draw me a cat", [], "alice") == "draw me a cat"
    assert await pi.resolve("draw it soon, not now", [], "alice") is None


async def test_the_keywords_scene_is_kept_with_the_subject(haiku):
    prior = turns(("user", "we are sitting on the beach at sunset"))
    assert await pi.resolve("show me", prior, "alice") == (
        "show me\n\nScene: we are sitting on the beach at sunset"
        "\n\nSubject: a red fox"
    )


async def test_a_long_subject_is_cut_and_tidied(haiku):
    answer, _ = haiku
    answer["text"] = json.dumps(
        {"draw": True, "subject": "a   red\nfox " + "x" * 500, "again": False}
    )
    result = await pi.resolve("draw it", [], "alice")
    subject = result.split("Subject: ", 1)[1]
    assert subject.startswith("a red fox x")
    assert len(subject) == pi.MAX_SUBJECT_CHARS


async def test_only_his_own_words_can_ask_for_undress(haiku):
    """Her replies must never unlock it, so a subject that adds it is dropped."""
    answer, _ = haiku
    answer["text"] = '{"draw": true, "subject": "her naked", "again": false}'
    prior = turns(("user", "hello"), ("assistant", "I could pose naked for you"))
    assert await pi.resolve("draw it", prior, "alice") == "draw it"


async def test_his_own_earlier_words_can_carry_it(haiku):
    answer, _ = haiku
    answer["text"] = '{"draw": true, "subject": "her after a bath", "again": false}'
    prior = turns(("user", "a picture of you after a bath"), ("assistant", "Hm?"))
    result = await pi.resolve("do it", prior, "alice")
    assert result == "do it\n\nSubject: her after a bath"


async def test_his_last_picture_request_can_carry_it(haiku):
    answer, _ = haiku
    answer["text"] = '{"draw": true, "subject": "her after a bath", "again": true}'
    pi.note_picture("alice", "delivered", "her after a bath")
    result = await pi.resolve("another one", [], "alice")
    assert result.startswith("another one\n\nSubject: her after a bath")


async def test_each_redo_is_a_new_picture(haiku):
    """The same words within ten minutes would hand back the same picture."""
    answer, _ = haiku
    answer["text"] = '{"draw": true, "subject": "a red fox", "again": true}'
    first = await pi.resolve("another one", [], "alice")
    pi.note_picture("alice", "delivered", first)
    second = await pi.resolve("another one", [], "alice")
    assert first != second
    assert "New version" in first and "New version" in second


def test_her_last_picture_is_forgotten_after_a_while(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pi.time, "monotonic", lambda: clock[0])
    pi.note_picture("alice", "failed", "x")
    assert pi.last_picture("alice")["outcome"] == "failed"
    clock[0] += pi.REMEMBER_PICTURE_S + 1
    assert pi.last_picture("alice") is None
    assert "alice" not in pi._LAST


def test_a_later_note_keeps_the_request_it_does_not_repeat():
    pi.note_picture("alice", "drawing", "draw a red fox")
    pi.note_picture("alice", "failed")
    assert pi.last_picture("alice") == {
        "outcome": "failed",
        "request": "draw a red fox",
        "versions": 0,
    }


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        (None, ""),
        ("drawing", "still being drawn"),
        ("delivered", "reached him"),
        ("failed", "failed"),
    ],
)
def test_she_knows_what_happened_to_her_last_picture(note, expected):
    if note:
        pi.note_picture("alice", note, "x")
    assert expected in pi.state_note("alice")


def test_every_status_has_wording():
    for outcome in ("drawing", "delivered", "failed"):
        pi.note_picture("bob", outcome, "x")
        assert pi.state_note("bob")


class TestWhatSheIsToldEachTurn:
    """She cannot see a picture start, arrive or fail. These lines are how she knows."""

    async def _prompt(self, content, monkeypatch, *, allowed="default", **pipeline_kw):
        from app.chat_handlers import _handle_message
        from tests.test_chat_handlers_coverage import (
            _drain_tasks,
            _fresh_session,
            _patched_pipeline,
        )

        monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", allowed)
        monkeypatch.delenv("KLUKAI_IMAGE_PROVIDER", raising=False)
        captured = {}

        async def _stream(system_prompt, messages, config):
            captured["sys"] = str(system_prompt)
            yield "Ok."

        with _patched_pipeline(**pipeline_kw) as ns:
            ns.router.stream = _stream
            with patch("app.chat_handlers.router", ns.router):
                await _handle_message(content, _fresh_session())
                await _drain_tasks()
        return captured["sys"], ns

    async def test_a_picture_underway_is_not_called_done(self, monkeypatch):
        prompt, ns = await self._prompt(
            "draw yourself", monkeypatch, needs_image_val=True
        )
        assert "being drawn for this request" in prompt
        assert "has not arrived yet" in prompt
        assert "Already done" not in prompt
        assert "No picture is being drawn" not in prompt
        ns.background_image_gen.assert_awaited_once()

    async def test_no_picture_underway_and_she_still_knows_she_can(self, monkeypatch):
        prompt, ns = await self._prompt("good morning", monkeypatch)
        assert "You can draw and send him pictures" in prompt
        assert "No picture is being drawn this turn" in prompt
        assert "Never say you can't draw" in prompt
        ns.background_image_gen.assert_not_called()

    async def test_she_is_not_promised_pictures_this_account_cannot_get(
        self, monkeypatch
    ):
        prompt, _ = await self._prompt("good morning", monkeypatch, allowed="someone")
        assert "You can draw and send him pictures" not in prompt

    async def test_she_hears_that_his_last_picture_failed(self, monkeypatch):
        pi.note_picture("default", "failed", "draw a red fox")
        prompt, _ = await self._prompt("good morning", monkeypatch)
        assert "Your last picture failed to draw" in prompt

    async def test_a_picture_he_asked_for_beats_a_recall_word(self, monkeypatch):
        """She was told it is coming, so "draw that picture" must draw."""
        _, ns = await self._prompt(
            "draw me that picture",
            monkeypatch,
            needs_image_val=True,
            wants_recall_val=True,
        )
        ns.background_image_gen.assert_awaited_once()
        ns.background_recall.assert_not_called()

    async def test_a_recall_still_recalls(self, monkeypatch):
        _, ns = await self._prompt(
            "remember that picture", monkeypatch, wants_recall_val=True
        )
        ns.background_recall.assert_awaited_once()
        ns.background_image_gen.assert_not_called()
