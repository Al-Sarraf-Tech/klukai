"""With Dominus powered off, every background text feature still works on Haiku.

Each test turns Haiku on (mocked: no real API calls), opens the Dominus
circuit and makes every local entry point raise, then checks the feature's
own output and the request it sent: the caller's prompt, its route name and
a budget that leaves room for adaptive thinking.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import background_llm as bg
from app import extraction_llm as xl
from app import fact_extractor as fe
from app.circuit_breakers import get_breaker

from .test_reflection_on_return import _FakePool, _mk_affection
from .test_weekly_reflection import _FakePool as _WeeklyPool


class _DeadGate:
    """The Dominus gate: entering it means a local call was attempted."""

    async def __aenter__(self):
        raise AssertionError("Dominus was called with its circuit open")

    async def __aexit__(self, *exc):
        return False


def _dead_router():
    router = MagicMock()
    router.complete_local = AsyncMock(side_effect=AssertionError("Dominus called"))
    router.route = AsyncMock(side_effect=AssertionError("Dominus called"))
    return router


@pytest.fixture
def haiku_only(monkeypatch):
    """Haiku on, Dominus off: the owner's usual night."""
    monkeypatch.setenv("KLUKAI_EXTRACTION_PROVIDER", "anthropic")
    monkeypatch.delenv("KLUKAI_EXTRACTION_MODEL", raising=False)
    complete = AsyncMock(return_value="")
    monkeypatch.setattr(xl, "complete", complete)
    dead = AsyncMock(side_effect=AssertionError("Dominus called"))
    monkeypatch.setattr(fe, "call_llm", dead)
    monkeypatch.setattr(fe, "call_llm_text", dead)
    monkeypatch.setattr("app.llm_json.call_llm", dead)
    monkeypatch.setattr("app.llm_json.call_llm_text", dead)
    breaker = get_breaker("lm_studio")
    breaker.force_open()
    with patch("app.llm_router.get_lm_gate", return_value=_DeadGate()):
        yield complete
    breaker.force_close()


def _sent(complete):
    args, kwargs = complete.await_args
    return args[0], kwargs


# ── per-turn: promises/events and episode journal entries ────────────────


async def test_promise_and_event_detection_on_haiku(haiku_only):
    haiku_only.return_value = json.dumps(
        {
            "promises": [
                {
                    "action": "fix the door",
                    "target": None,
                    "deadline_hint": "tomorrow",
                    "confidence": 0.9,
                },
                {"action": "maybe someday", "confidence": 0.3},
            ],
            "events": [
                {
                    "what": "job interview at the bank",
                    "when_hint": "Thursday at 9am",
                    "sensitivity": "normal",
                    "confidence": 0.95,
                }
            ],
        }
    )
    out = await fe.extract_promises("I'll fix the door tomorrow.", 6)
    assert out["promises"] == [
        {
            "action": "fix the door",
            "target": None,
            "deadline_hint": "tomorrow",
            "confidence": 0.9,
        }
    ]
    assert out["events"][0]["what"] == "job interview at the bank"
    prompt, kwargs = _sent(haiku_only)
    assert prompt == fe.PROMISE_PROMPT.format(
        user_message="I'll fix the door tomorrow."
    )
    assert kwargs == {"max_tokens": bg.haiku_budget(640), "route": "promises"}


async def test_promise_detection_with_nothing_available_is_empty(haiku_only):
    haiku_only.side_effect = xl.ExtractionUnavailable("cap")
    assert await fe.extract_promises("I'll call mom.", 6) == {
        "promises": [],
        "events": [],
    }


async def test_episode_summary_on_haiku(haiku_only):
    haiku_only.return_value = "He told me about the rain; I stayed close."
    turns = [
        {"role": "user", "content": "It's raining."},
        {"role": "assistant", "content": "Then stay in, Commander."},
        {"role": "user", "content": "Only if you stay too."},
    ]
    out = await fe.create_episode_summary(turns)
    assert out == "He told me about the rain; I stayed close."
    prompt, kwargs = _sent(haiku_only)
    assert prompt.startswith("Write a 1-2 sentence Klukai journal entry")
    assert "User: It's raining." in prompt
    assert kwargs == {"max_tokens": bg.haiku_budget(150), "route": "episode_summary"}


async def test_episode_summary_with_nothing_available_is_none(haiku_only):
    haiku_only.side_effect = xl.ExtractionLLMError("boom")
    turns = [{"role": "user", "content": "x"}] * 3
    assert await fe.create_episode_summary(turns) is None


# ── proactive lines ──────────────────────────────────────────────────────


async def test_mission_update_on_haiku(haiku_only):
    haiku_only.return_value = "Belka found the cache. Heading back soon."
    out = await fe.generate_mission_update("Recon the ridge", 40, 2, None, [], 7)
    assert out == "Belka found the cache. Heading back soon."
    prompt, kwargs = _sent(haiku_only)
    assert "Mission: Recon the ridge" in prompt
    assert kwargs["route"] == "mission_update"


async def test_mission_update_with_nothing_available_stays_empty(haiku_only):
    haiku_only.side_effect = xl.ExtractionUnavailable("cap")
    assert await fe.generate_mission_update("Recon", 40, 2, None, [], 7) == ""


async def test_romance_line_on_haiku_and_its_fallback(haiku_only):
    haiku_only.return_value = "The stars are out, Commander."
    assert (
        await fe.generate_romance_message(8, "tender", "", "evening")
        == "The stars are out, Commander."
    )
    assert _sent(haiku_only)[1]["route"] == "romance"
    haiku_only.side_effect = xl.ExtractionUnavailable("cap")
    assert (
        await fe.generate_romance_message(8, "tender", "", "evening")
        == "...The evening is quiet. I was thinking about today."
    )


async def test_dream_line_on_haiku_when_creative_text_is_local(haiku_only):
    from app.proactive import ProactiveEngine

    haiku_only.return_value = "I dreamed you were walking the perimeter with me."
    engine = ProactiveEngine()
    engine._affection_level = 5
    engine._on_message_callback = AsyncMock()
    with (
        patch("app.proactive.events.random.random", return_value=0.2),
        patch("app.memory_archive.list_memories", AsyncMock(return_value=[])),
    ):
        await engine._dream_event()
    engine._on_message_callback.assert_any_await(
        "I dreamed you were walking the perimeter with me."
    )
    prompt, kwargs = (
        haiku_only.await_args_list[0].args[0],
        haiku_only.await_args_list[0].kwargs,
    )
    assert prompt.startswith("You are Klukai, T-Doll squad leader.")
    assert kwargs == {"max_tokens": bg.haiku_budget(200), "route": "dream"}


async def test_dream_with_nothing_available_sends_nothing(haiku_only):
    from app.proactive import ProactiveEngine

    haiku_only.side_effect = xl.ExtractionUnavailable("cap")
    engine = ProactiveEngine()
    engine._affection_level = 5
    engine._on_message_callback = AsyncMock()
    with (
        patch("app.proactive.events.random.random", return_value=0.2),
        patch("app.memory_archive.list_memories", AsyncMock(return_value=[])),
        patch("app.memory_archive.recall_memory", AsyncMock(return_value=None)),
    ):
        await engine._dream_event()
    engine._on_message_callback.assert_not_awaited()
    assert engine._dream_delivered_today is False


async def test_memory_recall_on_haiku(haiku_only):
    from app.proactive import ProactiveEngine

    haiku_only.return_value = "Remember the rain on the deck, Commander?"
    engine = ProactiveEngine()
    engine._affection_level = 5
    engine._on_message_callback = AsyncMock()
    with patch(
        "app.memory_archive.recall_memory",
        AsyncMock(return_value={"annotation": "The observation deck in the rain."}),
    ):
        await engine._memory_recall_event()
    engine._on_message_callback.assert_awaited_once_with(
        "Remember the rain on the deck, Commander?"
    )
    prompt, kwargs = _sent(haiku_only)
    assert "Memory to recall (do not quote directly): The observation deck" in prompt
    assert kwargs["route"] == "memory_recall"


async def test_weekly_reflection_on_haiku_with_her_preamble(haiku_only):
    from app.proactive import ProactiveEngine
    import app.context as context_mod

    reflection = (
        "A quiet week. He came back every evening, and I found myself waiting "
        "for the door. I would like more of the rain and the coffee."
    )
    haiku_only.return_value = reflection
    engine = ProactiveEngine()
    pool = _WeeklyPool([[("alice",)]], [[("user", "hello")] * 15])
    memory = MagicMock()
    memory.store_episode = AsyncMock(return_value="ep")
    orig = context_mod.memory, context_mod.router
    context_mod.memory, context_mod.router = memory, _dead_router()
    try:
        with (
            patch("app.db.get_pool", return_value=pool),
            patch("app.personality.load_personality", return_value={}),
            patch(
                "app.personality.build_character_preamble",
                return_value="You are Klukai.",
            ),
        ):
            await engine._weekly_reflection()
    finally:
        context_mod.memory, context_mod.router = orig
    kwargs = memory.store_episode.await_args.kwargs
    assert kwargs["summary"] == reflection
    # companion_episodes.conversation_id is a UUID foreign key.
    assert kwargs["conversation_id"] is None
    prompt, sent = _sent(haiku_only)
    assert prompt.startswith("Write a private weekly reflection journal entry")
    assert sent == {
        "max_tokens": bg.haiku_budget(400),
        "route": "weekly_reflection",
        "system": "You are Klukai.",
    }


async def test_weekly_reflection_with_nothing_available_stores_nothing(haiku_only):
    from app.proactive import ProactiveEngine
    import app.context as context_mod

    haiku_only.side_effect = xl.ExtractionUnavailable("cap")
    engine = ProactiveEngine()
    pool = _WeeklyPool([[("alice",)]], [[("user", "hello")] * 15])
    memory = MagicMock()
    memory.store_episode = AsyncMock()
    orig = context_mod.memory, context_mod.router
    context_mod.memory, context_mod.router = memory, _dead_router()
    try:
        with (
            patch("app.db.get_pool", return_value=pool),
            patch("app.personality.load_personality", return_value={}),
            patch("app.personality.build_character_preamble", return_value="P"),
        ):
            await engine._weekly_reflection()
    finally:
        context_mod.memory, context_mod.router = orig
    memory.store_episode.assert_not_awaited()


async def test_return_greeting_on_haiku_with_her_preamble(haiku_only):
    from app.reflect_helpers import _maybe_reflect_on_return

    haiku_only.return_value = "Welcome back, Commander. You were telling me about rain."
    last = datetime.now(timezone.utc) - timedelta(hours=12)
    pool = _FakePool([(last,), [("user", "We talked about rain.")], ("tender",)])
    ws = MagicMock()
    ws.is_connected.return_value = True
    ws.send_proactive = AsyncMock()
    aff = MagicMock()
    aff.get_state = AsyncMock(return_value=_mk_affection())
    with (
        patch("app.db.get_pool", return_value=pool),
        patch("app.reflect_helpers.router", _dead_router()),
        patch("app.reflect_helpers.ws", ws),
        patch("app.reflect_helpers.affection", aff),
        patch("app.reflect_helpers.now_local", return_value=datetime(2026, 10, 14, 15)),
        patch("app.personality.load_personality", return_value={}),
        patch(
            "app.personality.build_character_preamble",
            return_value="You are Klukai.",
        ),
        patch("app.voice_archive.save_voice_note", AsyncMock(return_value=None)),
        patch("asyncio.sleep", AsyncMock()),
    ):
        await _maybe_reflect_on_return("alice")
    ws.send_proactive.assert_awaited_once_with(
        "alice", "Welcome back, Commander. You were telling me about rain."
    )
    prompt, sent = _sent(haiku_only)
    assert "Last exchanges (resume from here):" in prompt
    assert sent == {
        "max_tokens": bg.haiku_budget(180),
        "route": "return_greeting",
        "system": "You are Klukai.",
    }


async def test_daily_recap_on_haiku(haiku_only):
    from app import main

    from .test_main_coverage import _recap_pool

    haiku_only.return_value = "Today we talked about the ridge and coffee."
    pool = _recap_pool([("user", f"msg {i}") for i in range(8)])
    with (
        patch.object(main, "get_pool", return_value=pool),
        patch("app.context.router.route", AsyncMock(side_effect=AssertionError)),
    ):
        out = await main.generate_daily_recap(affection_level=5)
    assert out == "Today we talked about the ridge and coffee."
    prompt, sent = _sent(haiku_only)
    assert "Write warmly" in prompt
    assert sent == {"max_tokens": bg.haiku_budget(2048), "route": "daily_recap"}


async def test_daily_recap_with_nothing_available_is_none(haiku_only):
    from app import main

    from .test_main_coverage import _recap_pool

    haiku_only.side_effect = xl.ExtractionUnavailable("cap")
    pool = _recap_pool([("user", f"msg {i}") for i in range(8)])
    with patch.object(main, "get_pool", return_value=pool):
        assert await main.generate_daily_recap(affection_level=5) is None


# ── memory writing: Her POV, archive captions, legacy classifier ─────────


async def test_her_pov_scene_on_haiku_when_creative_text_is_local(haiku_only):
    from app.memory_her_pov import compose_pov

    haiku_only.return_value = json.dumps(
        {
            "annotation": "I kept the rain between us.",
            "scene_tags": "hangar, rain, silver hair",
            "couple": True,
            "mood": "tender",
            "title": "Rain at Home",
        }
    )
    out = await compose_pov({"user_content": "Rain", "assistant_content": "Home"}, 5)
    assert out["annotation"] == "I kept the rain between us."
    assert out["scene_tags"] == "hangar, rain, silver hair"
    assert out["couple"] is True
    assert _sent(haiku_only)[1]["route"] == "her_pov"


async def test_her_pov_with_nothing_available_keeps_its_defaults(haiku_only):
    from app.memory_her_pov import compose_pov

    haiku_only.side_effect = xl.ExtractionUnavailable("cap")
    out = await compose_pov({"user_content": "Rain", "assistant_content": "Home"}, 5)
    assert out["annotation"].startswith("...I pulled this from our records.")
    assert out["title"] == "From my side"


async def test_archive_caption_backfill_on_haiku(haiku_only):
    from app.memory_archive import backfill_annotations

    from .test_memory_archive_coverage import _conn_ctx, _RecordingConn

    haiku_only.return_value = '"The rooftop was ours that night."'
    select = _RecordingConn(rows=[("mem-1", "rooftop", "Quiet Hours", ["night"])])
    update = _RecordingConn()
    with (
        patch("app.memory_archive.get_conn", side_effect=lambda: _conn_ctx(select)()),
        patch(
            "app.memory_archive.get_conn_autocommit",
            side_effect=lambda: _conn_ctx(update)(),
        ),
    ):
        result = await backfill_annotations(user_id="alice")
    assert result == {"total": 1, "updated": 1}
    assert update.calls[0][1] == ("The rooftop was ours that night.", "mem-1")
    assert _sent(haiku_only)[1]["route"] == "archive_caption"


async def test_archive_backfill_with_nothing_available_writes_no_placeholder(
    haiku_only,
):
    from app.memory_archive import backfill_annotations

    from .test_memory_archive_coverage import _conn_ctx, _RecordingConn

    haiku_only.side_effect = xl.ExtractionUnavailable("cap")
    select = _RecordingConn(rows=[("mem-1", "rooftop", "Quiet Hours", None)])
    update = _RecordingConn()
    with (
        patch("app.memory_archive.get_conn", side_effect=lambda: _conn_ctx(select)()),
        patch(
            "app.memory_archive.get_conn_autocommit",
            side_effect=lambda: _conn_ctx(update)(),
        ),
    ):
        result = await backfill_annotations(user_id="alice")
    assert result == {"total": 1, "updated": 0}
    assert update.calls == []


async def test_legacy_affection_classifier_on_haiku(haiku_only):
    from app.affection_classifier import CLASSIFICATION_PROMPT, classify_interaction

    haiku_only.return_value = '{"type": "compliment", "intensity": 12}'
    out = await classify_interaction(
        None,
        "You did well today.",
        url="http://unused",
        model="unused",
        prompt_template=CLASSIFICATION_PROMPT,
    )
    assert out == ("compliment", 10)
    assert _sent(haiku_only)[1]["route"] == "affection_classification"


@pytest.mark.parametrize("reply", [None, '{"type": "neutral", "intensity": "very"}'])
async def test_legacy_classifier_failures_are_neutral(haiku_only, reply):
    from app.affection_classifier import CLASSIFICATION_PROMPT, classify_interaction

    if reply is None:
        haiku_only.side_effect = xl.ExtractionUnavailable("cap")
    else:
        haiku_only.return_value = reply
    out = await classify_interaction(
        None, "hm", url="u", model="m", prompt_template=CLASSIFICATION_PROMPT
    )
    assert out == ("neutral", 5)
