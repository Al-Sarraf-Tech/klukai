"""Prompt reads overlap without changing memory filtering or block ordering."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.chat_prompt import ChatPromptBuilder, OutfitContext
from app.models import SessionState
from app.personality.system_prompt import PromptText


@pytest.fixture
def prompt_builder(monkeypatch):
    from app import context, decisions, picture_intent, rituals, tributes

    state = SimpleNamespace(
        level=3,
        score=300,
        first_interaction=None,
        last_interaction_date=None,
        consecutive_days=0,
    )
    memory = SimpleNamespace(
        recall_for_prompt=AsyncMock(return_value=([], {}, [])),
        get_memory_nudge=AsyncMock(return_value=None),
        get_inside_jokes=AsyncMock(return_value=[]),
    )
    captured = {}

    def assemble(**kwargs):
        captured.update(kwargs)
        return PromptText("PERSONA\n\nCONTEXT", "PERSONA")

    builder = ChatPromptBuilder(
        memory=memory,
        affection=SimpleNamespace(get_state=AsyncMock(return_value=state)),
        proactive=SimpleNamespace(
            get_mission_timer=lambda user: None,
            check_anniversaries=AsyncMock(return_value=[]),
            get_comfort_objects=AsyncMock(return_value=[]),
        ),
        router=SimpleNamespace(is_game_active=AsyncMock(return_value=False)),
        ws=None,
        assemble_prompt=assemble,
        personality=dict,
        wants_thread=lambda text: False,
        needs_image=lambda text: False,
        hours_away=AsyncMock(return_value=2),
        warm_budget=1,
        turn_budget=1,
    )
    monkeypatch.setattr(
        builder,
        "wardrobe_turn",
        AsyncMock(return_value=OutfitContext(None, None, None, "")),
    )
    monkeypatch.setattr(
        context,
        "physical",
        SimpleNamespace(get_state=AsyncMock(return_value=("normal", ""))),
    )
    monkeypatch.setattr(tributes, "get_crown_jewel", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.night_watch.night_watch_prompt_block", AsyncMock(return_value="WATCH")
    )

    # The world block must see the effects of the decision resolved this turn.
    world = {"value": "old"}

    async def decision(*args):
        world["value"] = "updated"
        return "DECISION"

    async def her_world(*args):
        return f"WORLD {world['value']}"

    monkeypatch.setattr(decisions, "resolve_prompt_block", decision)
    monkeypatch.setattr(decisions, "her_world_prompt_block", her_world)
    monkeypatch.setattr(
        rituals, "birthday_prompt_block", AsyncMock(return_value="BIRTHDAY")
    )
    monkeypatch.setattr("app.chat_prompt.pictures_available", lambda user: False)
    monkeypatch.setattr(picture_intent, "state_note", lambda user: "")
    monkeypatch.setattr(
        "app.conversation_continuity.load_recent_assistant_replies",
        AsyncMock(return_value=[]),
    )
    return builder, captured


async def test_history_and_presence_start_while_recall_is_pending(
    prompt_builder, monkeypatch
):
    builder, _ = prompt_builder
    started = {name: asyncio.Event() for name in ("recall", "history", "presence")}
    release = asyncio.Event()

    async def recall(*args, **kwargs):
        started["recall"].set()
        await release.wait()
        return [], {}, []

    async def history(user_id, limit):
        assert (user_id, limit) == ("alice", 20)
        started["history"].set()
        await release.wait()
        return []

    async def hours_away(user_id):
        assert user_id == "alice"
        started["presence"].set()
        await release.wait()
        return 2

    builder.memory.recall_for_prompt = recall
    builder.hours_away = hours_away
    monkeypatch.setattr(
        "app.conversation_continuity.load_recent_assistant_replies", history
    )
    pending = asyncio.create_task(
        builder.build(
            "Tell me about our earlier conversation",
            SessionState(conversation_id="test-conversation"),
            "alice",
        )
    )
    try:
        # Event dependencies prove overlap; the deadline only guards deadlock.
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in started.values())), 1
        )
        assert not pending.done()
        release.set()
        assert (await pending).stable == "PERSONA"
    finally:
        release.set()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_parallel_prompt_keeps_prefix_memory_filter_and_block_order(
    prompt_builder, monkeypatch
):
    builder, captured = prompt_builder
    repeated = "We watched the sunrise from the north bridge."
    kept = "He prefers jasmine tea in the evening."
    builder.memory.recall_for_prompt = AsyncMock(
        return_value=(
            [repeated, kept],
            {"name": "Alice"},
            [
                {"assistant_content": repeated},
                {"assistant_content": kept},
            ],
        )
    )
    monkeypatch.setattr(
        "app.conversation_continuity.load_recent_assistant_replies",
        AsyncMock(return_value=[repeated]),
    )

    prompt = await builder.build(
        "Tell me about our earlier conversation",
        SessionState(conversation_id="test-conversation"),
        "alice",
    )
    assert captured["memories"] == [kept]
    assert captured["recalled_exchanges"] == [{"assistant_content": kept}]
    assert captured["relationship_facts"] == {"name": "Alice"}
    assert prompt.stable == "PERSONA"
    assert prompt == (
        "PERSONA\n\nCONTEXT\n\n"
        "PRESENCE (color your opening; do not recite these bullets):\n"
        "It has been about 2 hours since you last spoke. "
        "Register it lightly if natural — no lecture.\n\n"
        "THIS TURN: Open with what you have to say, not a pause or a stage "
        "direction, unless this moment calls for one.\n\n"
        "WATCH\n\nDECISION\n\nWORLD updated\n\nBIRTHDAY"
    )


async def test_cancelling_prompt_joins_history_and_presence(
    prompt_builder, monkeypatch
):
    builder, _ = prompt_builder
    started = {name: asyncio.Event() for name in ("history", "presence")}
    finished = set()
    never = asyncio.Event()

    async def blocked(name):
        started[name].set()
        try:
            await never.wait()
        finally:
            finished.add(name)

    async def history(*args):
        return await blocked("history")

    async def hours_away(*args):
        return await blocked("presence")

    builder.hours_away = hours_away
    monkeypatch.setattr(
        "app.conversation_continuity.load_recent_assistant_replies", history
    )
    pending = asyncio.create_task(
        builder.build(
            "Tell me about our earlier conversation",
            SessionState(conversation_id="test-conversation"),
            "alice",
        )
    )
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in started.values())), 1
        )
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert finished == {"history", "presence"}
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize(
    "history_fails,presence_fails", [(True, False), (False, True), (True, True)]
)
async def test_failed_reads_keep_other_context_and_session_echo_filter(
    prompt_builder, monkeypatch, history_fails, presence_fails
):
    builder, captured = prompt_builder
    stored_reply = "We watched the sunrise from the north bridge."
    session_reply = "Yesterday we repaired the old motorbike together."
    kept = "He prefers jasmine tea in the evening."
    builder.memory.recall_for_prompt = AsyncMock(
        return_value=([stored_reply, session_reply, kept], {"name": "Alice"}, [])
    )

    async def history(*args):
        if history_fails:
            raise RuntimeError("archive unavailable")
        return [stored_reply]

    async def hours_away(*args):
        if presence_fails:
            raise RuntimeError("presence unavailable")
        return 2

    builder.hours_away = hours_away
    monkeypatch.setattr(
        "app.conversation_continuity.load_recent_assistant_replies", history
    )
    session = SessionState(
        conversation_id="test-conversation",
        turns=[{"role": "assistant", "content": session_reply}],
    )
    prompt = await builder.build(
        "Tell me about our earlier conversation", session, "alice"
    )
    assert captured["memories"] == ([stored_reply, kept] if history_fails else [kept])
    assert captured["relationship_facts"] == {"name": "Alice"}
    assert ("PRESENCE (" in prompt) is (not presence_fails)
    assert prompt.stable == "PERSONA"
    assert prompt.endswith("WATCH\n\nDECISION\n\nWORLD updated\n\nBIRTHDAY")
