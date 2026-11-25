"""Extraction attempts that produce nothing usable are not kept.

A reserved attempt is released on failure, timeout, an empty reply, and
malformed JSON. Dollars for a request that was sent stay charged. One
fallback may reserve again only because the first attempt was released.
Her reply and a filled-in default do not score affection. His words can.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import extraction_llm as xl
from app.creative_budget import CreativeBudgetError
from app.extraction_llm import ExtractionCancelled, ExtractionLLMError
from app.fact_extractor import _DEFAULT_RESULT, extract_facts
from app.memory_extraction import (
    ExtractionRuntime,
    mention_without_commitment,
    score_commander_interaction,
)
from app.models import SessionState
from app.xai_spend import anthropic_usd, spent_today


def _message(text: str, **overrides) -> dict:
    body = {
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 900, "output_tokens": 300},
    }
    body.update(overrides)
    return body


def _rows(path) -> list[str]:
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        try:
            found = conn.execute("SELECT id FROM creative_attempts").fetchall()
        except sqlite3.OperationalError:
            return []
    return [row[0] for row in found]


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    path = tmp_path / "attempts.sqlite3"
    monkeypatch.setenv("KLUKAI_CREATIVE_LEDGER", str(path))
    monkeypatch.setenv("KLUKAI_EXTRACTION_DAILY_LIMIT", "1")
    monkeypatch.setenv("KLUKAI_CLAUDE_PRIMARY", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-extraction-key")
    monkeypatch.delenv("KLUKAI_EXTRACTION_MODEL", raising=False)
    monkeypatch.setattr(xl, "_warned", set())
    monkeypatch.setattr(xl, "_cap_logged_day", None)
    return path


def _response(reply, request):
    if callable(reply):
        reply = reply(request)
    if isinstance(reply, httpx.Response):
        return reply
    return httpx.Response(200, json=reply, request=request)


@pytest.fixture
async def haiku(monkeypatch, ledger):
    """Local Messages responses. Nothing is sent to the network."""
    state = SimpleNamespace(requests=[], reply=_message('{"mood": "amused"}'), path=ledger)

    async def send(self, request, *args, **kwargs):
        state.requests.append(request)
        return _response(state.reply, request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    old = xl._http
    xl._http = None
    if old is not None and not old.is_closed:
        await old.aclose()
    yield state
    client = xl._http
    xl._http = None
    if client is not None and not client.is_closed:
        await client.aclose()


def _effort(request) -> str:
    return json.loads(request.content)["output_config"]["effort"]


async def test_release_returns_only_that_attempt(ledger):
    from app import creative_budget as budget

    first = await budget.reserve_attempt("extraction")
    with pytest.raises(CreativeBudgetError):
        await budget.reserve_attempt("extraction")
    assert await budget.release_attempt(first) is True
    assert _rows(ledger) == []
    second = await budget.reserve_attempt("extraction")
    assert second != first
    assert await budget.release_attempt("missing") is False
    assert _rows(ledger) == [second]


async def test_http_failure_and_timeout_release_the_attempt(haiku, ledger):
    haiku.reply = lambda request: httpx.Response(500, request=request)
    with pytest.raises(ExtractionLLMError):
        await xl.complete_claude("x", max_tokens=100, route="extraction")
    assert _rows(ledger) == []
    assert spent_today() == 0
    assert len(haiku.requests) == 1

    haiku.requests.clear()

    async def time_out(self, request, *args, **kwargs):
        haiku.requests.append(request)
        raise TimeoutError()

    xl._http = None
    with patch.object(httpx.AsyncClient, "send", time_out):
        with pytest.raises(ExtractionLLMError):
            await xl.complete_claude("x", max_tokens=100, route="extraction", timeout=1)
    assert _rows(ledger) == []
    assert len(haiku.requests) == 1
    # The freed slot can be reserved again. No second charge was invented.
    assert await xl.reserve_attempt("extraction")


async def test_empty_billed_reply_releases_the_counter_and_keeps_the_dollars(haiku, ledger):
    haiku.reply = _message("   ")
    with pytest.raises(ExtractionLLMError):
        await xl.complete_claude("x", max_tokens=100, route="extraction")
    assert _rows(ledger) == []
    assert spent_today() == pytest.approx(
        anthropic_usd({"input_tokens": 900, "output_tokens": 300})
    )
    assert len(haiku.requests) == 1
    assert _effort(haiku.requests[0]) == "low"


async def test_usable_reply_still_spends_one_attempt(haiku, ledger):
    assert await xl.complete_claude("x", max_tokens=100, route="extraction") == '{"mood": "amused"}'
    assert len(_rows(ledger)) == 1
    assert len(haiku.requests) == 1
    assert _effort(haiku.requests[0]) == "low"
    with pytest.raises(CreativeBudgetError):
        await xl.reserve_attempt("extraction")


async def test_malformed_json_releases_and_one_fallback_uses_that_slot(haiku, ledger, monkeypatch):
    """The paid call is not repeated. The CLI retry reserves the released slot."""
    from app.grok_chat import Turn

    haiku.reply = _message("not json")
    seen: list[str] = []

    async def send(self, request, *args, **kwargs):
        haiku.requests.append(request)
        seen.extend(_rows(ledger))
        return _response(haiku.reply, request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    calls = []

    async def complete_turn(system, prompt, *, timeout=60, role="chat"):
        calls.append(prompt)
        return Turn(
            json.dumps(
                {
                    "mood": "amused",
                    "interaction": {"type": "playful", "intensity": 4},
                    "facts": [],
                }
            ),
            "grok-4.7",
            "low",
            "stream",
        )

    monkeypatch.setattr("app.grok_chat.configured", lambda: True)
    monkeypatch.setattr("app.grok_chat.complete_turn", complete_turn)
    out = await extract_facts("hi", "you look tender")
    assert out["mood"] == "amused"
    assert out["interaction"]["type"] == "playful"
    assert not out.get("mood_defaulted")
    assert len(haiku.requests) == 1
    assert _effort(haiku.requests[0]) == "low"
    assert len(calls) == 1
    assert seen and seen[0] not in _rows(ledger)
    assert len(_rows(ledger)) == 1
    # One billed Haiku reply, not a second paid try.
    assert spent_today() == pytest.approx(
        anthropic_usd({"input_tokens": 900, "output_tokens": 300})
    )


async def test_two_malformed_replies_spend_nothing(haiku, ledger, monkeypatch):
    from app.grok_chat import Turn

    haiku.reply = _message("nope")

    async def complete_turn(system, prompt, *, timeout=60, role="chat"):
        return Turn("still nope", "grok-4.7", "low", "stream")

    monkeypatch.setattr("app.grok_chat.configured", lambda: True)
    monkeypatch.setattr("app.grok_chat.complete_turn", complete_turn)
    out = await extract_facts("hi", "hello")
    assert out == dict(_DEFAULT_RESULT)
    assert _rows(ledger) == []
    assert len(haiku.requests) == 1
    assert await xl.reserve_attempt("extraction")


async def test_cli_prose_keeps_its_attempt_and_bad_json_does_not(ledger, monkeypatch):
    from app.grok_chat import GrokChatCancelled, Turn

    monkeypatch.setenv("KLUKAI_EXTRACTION_DAILY_LIMIT", "3")

    replies = []

    async def complete_turn(system, prompt, *, timeout=60, role="chat"):
        text = replies.pop(0)
        if isinstance(text, Exception):
            raise text
        return Turn(text, "grok-4.7", "low", "stream")

    monkeypatch.setattr("app.grok_chat.configured", lambda: True)
    monkeypatch.setattr("app.grok_chat.complete_turn", complete_turn)
    replies.append("Hello, Commander.")
    assert await xl.complete("Say hello.", max_tokens=100, route="romance") == "Hello, Commander."
    assert len(_rows(ledger)) == 1
    held = _rows(ledger)[0]

    replies.append("nope")
    with pytest.raises(ExtractionLLMError, match="Malformed JSON"):
        await xl.complete("Return ONLY valid JSON", max_tokens=100, route="extraction")
    assert _rows(ledger) == [held]

    replies.append(GrokChatCancelled("preempted"))
    with pytest.raises(ExtractionCancelled):
        await xl.complete("Say hello again.", max_tokens=100, route="romance")
    assert _rows(ledger) == [held]


def test_her_reply_does_not_score_and_his_words_do():
    hers = {
        "mood": "tender",
        "interaction": {"type": "tender", "intensity": 8},
    }
    assert score_commander_interaction(hers, "ok", "you look tender") is False
    assert score_commander_interaction(hers, "a tender night", "noted") is True
    quoted = {
        "interaction": {"type": "compliment", "intensity": 8, "quote": "black coffee"},
    }
    assert score_commander_interaction(quoted, "hi", "your black coffee") is False
    assert score_commander_interaction(quoted, "I drink black coffee", "noted") is True
    assert (
        score_commander_interaction(
            {"interaction": {"type": "compliment", "intensity": 8}, "interaction_defaulted": True},
            "you're amazing",
            "thank you",
        )
        is False
    )
    assert score_commander_interaction(hers, "  ", "you look tender") is False


def test_a_partial_reading_is_marked_instead_of_looking_finished():
    # The flags are set by extract_facts. This locks the shape the live path reads.
    assert _DEFAULT_RESULT["mood"] == "composed"
    assert "mood_defaulted" not in _DEFAULT_RESULT


async def test_partial_extract_does_not_apply_defaults_or_her_score(monkeypatch):
    monkeypatch.setattr("app.memory_extraction.asyncio.sleep", AsyncMock())

    async def fake_extract(user, assistant, image_generated=False, affection_level=0, report=None):
        if report is not None:
            report.outcome = "ok"
            report.provider = "anthropic"
            report.model = "claude-haiku-5-5"
        if user == "ok":
            return {
                "mood": "amused",
                "interaction": {"type": "tender", "intensity": 8},
                "facts": [],
                "topics": [],
                "should_remember": False,
            }
        if "tender" in user:
            return {
                "mood": "amused",
                "interaction": {"type": "tender", "intensity": 4},
                "facts": [],
                "topics": [],
                "should_remember": False,
            }
        return {
            "mood": "composed",
            "mood_defaulted": True,
            "interaction": {"type": "neutral", "intensity": 5},
            "interaction_defaulted": True,
            "facts": [
                {"key": "favorite_drink", "value": "black coffee"},
                {"key": "city", "value": "Amarillo"},
            ],
            "topics": [],
            "should_remember": False,
        }

    affection = SimpleNamespace(
        get_state=AsyncMock(return_value=SimpleNamespace(level=3)),
        apply_classification=AsyncMock(
            return_value=SimpleNamespace(
                delta=1,
                reason="tender",
                new_score=10,
                new_level=2,
                new_level_name="known",
                level_changed=False,
                level_direction="",
            )
        ),
    )
    memory = SimpleNamespace(
        set_relationship_fact=AsyncMock(),
        set_inside_joke=AsyncMock(),
        record_milestone=AsyncMock(),
        store_exchange=AsyncMock(),
        store_episode=AsyncMock(),
    )
    runtime = ExtractionRuntime(
        memory=memory,
        affection=affection,
        proactive=SimpleNamespace(
            record_first=AsyncMock(),
            store_gift=AsyncMock(),
            set_affection_level=lambda *args, **kwargs: None,
        ),
        ws=SimpleNamespace(
            send_heartbeat=AsyncMock(),
            send_heartbeat_spike=AsyncMock(),
            send_affection=AsyncMock(),
        ),
        update_curation=AsyncMock(),
        extract_facts=fake_extract,
        create_summary=AsyncMock(),
        apply_state=AsyncMock(return_value=True),
        deliver_oath=AsyncMock(),
        personality=lambda: {},
    )
    session = SessionState(conversation_id="c", mood="focused", turn_count=2)
    with (
        patch(
            "app.fact_extractor.extract_promises",
            AsyncMock(return_value={"promises": [], "events": []}),
        ),
        patch("app.op_brief.on_turn", AsyncMock()),
    ):
        await runtime.process("ok", "you look tender", session, user_id="alice")
        runtime.apply_state.assert_awaited()
        assert runtime.apply_state.await_args.args[1] == "amused"
        affection.apply_classification.assert_not_awaited()

        affection.apply_classification.reset_mock()
        runtime.apply_state.reset_mock()
        await runtime.process("a tender night", "noted", session, user_id="alice")
        affection.apply_classification.assert_awaited()
        assert affection.apply_classification.await_args.args[0] == "tender"

        memory.set_relationship_fact.reset_mock()
        runtime.apply_state.reset_mock()
        affection.apply_classification.reset_mock()
        await runtime.process(
            "I live in Amarillo", "You and your black coffee.", session, user_id="alice"
        )
        runtime.apply_state.assert_not_awaited()
        affection.apply_classification.assert_not_awaited()
        memory.set_relationship_fact.assert_awaited_once_with(
            "city", "Amarillo", user_id="alice"
        )


async def test_extract_facts_flags_a_partial_object_and_keeps_round1_filters(monkeypatch):
    async def fake_json(caller, prompt, **kwargs):
        report = kwargs.get("report")
        if report is not None:
            report.outcome = "ok"
            report.provider = "grok"
            report.model = "grok-4.7"
        return {"facts": [{"key": "city", "value": "Amarillo"}]}

    monkeypatch.setattr("app.background_llm.json_object", fake_json)
    out = await extract_facts("I live in Amarillo", "noted")
    assert out["mood"] == "composed"
    assert out["interaction"]["type"] == "neutral"
    assert out["mood_defaulted"] is True
    assert out["interaction_defaulted"] is True
    assert score_commander_interaction(out, "I live in Amarillo", "noted") is False

    assert mention_without_commitment("I mentioned the interview")
    assert not mention_without_commitment("I'll fix the door tomorrow")


async def test_a_mentioned_promise_is_still_not_stored():
    stored = AsyncMock()
    runtime = ExtractionRuntime(
        memory=SimpleNamespace(),
        affection=SimpleNamespace(),
        proactive=SimpleNamespace(),
        ws=SimpleNamespace(),
        update_curation=AsyncMock(),
        extract_facts=AsyncMock(),
        create_summary=AsyncMock(),
        apply_state=AsyncMock(),
        deliver_oath=AsyncMock(),
        personality=lambda: {},
    )
    with (
        patch(
            "app.fact_extractor.extract_promises",
            AsyncMock(
                return_value={
                    "promises": [{"action": "call your mother", "confidence": 0.9}],
                    "events": [],
                }
            ),
        ),
        patch("app.promises.store_promise", stored),
        patch("app.op_brief.on_turn", AsyncMock()),
    ):
        await runtime.store_commitments(
            "I mentioned I'd call her", 5, "alice", "You promised to call your mother."
        )
    stored.assert_not_awaited()
