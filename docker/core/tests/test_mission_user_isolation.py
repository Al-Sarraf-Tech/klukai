"""Private mission context and delivery must stay with the mission owner."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import chat_handlers, chat_prompt, main
from app.models import SessionState
from app.ws_manager import WSManager
from app.proactive import MissionTimer, ProactiveEngine
from .test_chat_handlers_coverage import _patched_pipeline
from .test_main_coverage import lifespan_mocks  # noqa: F401

PRIVATE = "ALICE_PRIVATE_MISSION_SENTINEL"
PRIMARY = "jalsarraf"


class PromptCaptured(Exception):
    pass


def _timer(description=PRIVATE):
    timer = MissionTimer()
    timer.active = True
    timer.mission_description = description
    return timer


async def _capture_prompt(
    engine, user_id, content="hello", session=None, *, start=False
):
    """Exercise the real chat path until prompt assembly; never call a provider."""
    engine.check_anniversaries = AsyncMock(return_value=[])
    engine.get_comfort_objects = AsyncMock(return_value=[])
    engine.record_first = AsyncMock()
    with (
        _patched_pipeline(),
        patch.object(chat_handlers, "proactive", engine),
        patch.object(chat_handlers, "_wants_mission_start", return_value=start),
        patch.object(chat_prompt.wardrobe, "ensure_today", AsyncMock()),
        patch.object(
            chat_prompt.her_day,
            "her_now",
            AsyncMock(side_effect=RuntimeError("offline test")),
        ),
        patch.object(
            chat_handlers, "assemble_system_prompt", side_effect=PromptCaptured
        ) as assemble,
    ):
        with pytest.raises(PromptCaptured):
            await chat_handlers._handle_message(
                content,
                session or SessionState(conversation_id=user_id),
                user_id=user_id,
            )
    return assemble.call_args.kwargs


@pytest.mark.asyncio
async def test_alice_mission_never_enters_bob_prompt():
    engine = ProactiveEngine()
    alice = _timer()
    engine._mission_timers["alice"] = alice
    # Simulate the old shared alias, which must never be consulted for Bob.
    engine._mission_timer = alice
    bob_prompt = await _capture_prompt(engine, "bob")
    alice_prompt = await _capture_prompt(engine, "alice")
    assert bob_prompt["mission_description"] is None
    assert PRIVATE not in str(bob_prompt)
    assert alice_prompt["mission_description"] == PRIVATE


@pytest.mark.asyncio
async def test_chat_starts_mission_for_authenticated_owner():
    engine = ProactiveEngine()
    with patch.object(MissionTimer, "_tick_loop", AsyncMock()):
        try:
            await _capture_prompt(engine, "alice", "keep me posted", start=True)
            assert "alice" in engine._mission_timers
            assert PRIMARY not in engine._mission_timers
        finally:
            for timer in engine._mission_timers.values():
                timer.stop()


@pytest.mark.asyncio
async def test_bob_cancelling_nonexistent_mission_cannot_stop_alice():
    engine = ProactiveEngine()
    alice = _timer()
    engine._mission_timers["alice"] = alice
    engine._mission_timer = alice
    with patch.object(engine, "_set_post_mission_physical", AsyncMock()):
        engine.stop_mission("bob", trigger_aftermath=False)
    assert alice.active
    assert engine._mission_timers["alice"] is alice
    assert engine._mission_timer is alice


@pytest.mark.asyncio
async def test_timer_updates_are_delivered_only_to_the_owner():
    engine = ProactiveEngine()
    deliveries = []

    async def deliver(message, user_id=PRIMARY):
        deliveries.append((user_id, message))

    engine.set_callback(deliver)
    with (
        patch.object(MissionTimer, "_tick_loop", AsyncMock()),
        patch(
            "app.fact_extractor.generate_mission_update",
            AsyncMock(return_value=PRIVATE),
        ),
    ):
        engine.start_mission(PRIVATE, user_id="alice")
        try:
            await engine._mission_timers["alice"]._fire_update(30, None)
        finally:
            engine._mission_timers["alice"].stop()
    assert deliveries == [("alice", PRIVATE)]


@pytest.mark.asyncio
async def test_secondary_mission_does_not_replace_scheduled_primary_context():
    engine = ProactiveEngine()
    with patch.object(MissionTimer, "_tick_loop", AsyncMock()):
        engine.start_mission("primary mission", user_id=PRIMARY)
        primary_timer = engine._mission_timers[PRIMARY]
        engine.start_mission(PRIVATE, user_id="alice")
        try:
            assert engine._mission_timer is primary_timer
        finally:
            for timer in engine._mission_timers.values():
                timer.stop()


def test_secondary_mood_and_affection_do_not_change_primary_schedule():
    engine = ProactiveEngine()
    engine.set_affection_level(2, user_id=PRIMARY)
    engine.set_last_mood("composed", user_id=PRIMARY)
    engine.set_affection_level(9, user_id="alice")
    engine.set_last_mood("battle_ready", user_id="alice")
    assert engine._affection_level == 2
    assert engine._last_mood == "composed"
    assert engine._affection_levels["alice"] == 9
    assert engine._moods["alice"] == "battle_ready"


@pytest.mark.asyncio
async def test_default_proactive_push_is_primary_only(monkeypatch):
    ws = SimpleNamespace(send_proactive=AsyncMock(), is_connected=lambda uid: False)
    push = AsyncMock(return_value=1)
    monkeypatch.setattr(main, "ws", ws)
    monkeypatch.setattr(main, "send_push", push)
    assert await main.proactive_callback("primary message")
    assert push.call_args.kwargs["user_id"] == PRIMARY


@pytest.mark.asyncio
async def test_mission_callback_persists_and_pushes_to_its_owner(monkeypatch):
    ws = SimpleNamespace(send_proactive=AsyncMock(), is_connected=lambda uid: False)
    push = AsyncMock(return_value=1)
    monkeypatch.setattr(main, "ws", ws)
    monkeypatch.setattr(main, "send_push", push)
    assert await main.proactive_callback(PRIVATE, user_id="alice")
    ws.send_proactive.assert_awaited_once_with("alice", PRIVATE)
    assert push.call_args.kwargs["user_id"] == "alice"


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [None, "bob"])
async def test_session_getter_never_falls_back_to_another_user(
    lifespan_mocks, monkeypatch, requested
):
    captured = {}
    lifespan_mocks.proactive.set_session_getter.side_effect = lambda fn: (
        captured.setdefault("getter", fn)
    )
    manager = WSManager()
    manager._connections = {"alice": {MagicMock(close=AsyncMock())}}
    monkeypatch.setattr(main, "ws", manager)
    private_session = SessionState(conversation_id=PRIVATE)
    lifespan_mocks.memory.get_session.side_effect = lambda sid: (
        private_session if sid == "session:alice" else None
    )
    async with main.lifespan(main.app):
        result = await captured["getter"](requested)
    assert result is None
    assert [
        call.args[0] for call in lifespan_mocks.memory.get_session.await_args_list
    ] == [f"session:{requested or PRIMARY}"]


@pytest.mark.asyncio
async def test_decompression_uses_owners_affection_and_delivery():
    engine = ProactiveEngine()
    engine._affection_level = 9
    engine._affection_levels["alice"] = 0
    deliveries = []

    async def deliver(message, user_id=PRIMARY):
        deliveries.append((user_id, message))

    engine.set_callback(deliver)
    with (
        patch.object(engine, "_can_send", return_value=True),
        patch("app.proactive.mission.asyncio.sleep", AsyncMock()),
        patch("app.proactive.mission._decompression_normal", return_value=["normal"]),
        patch(
            "app.proactive.mission._decompression_intimate_addons",
            return_value=["intimate"],
        ),
    ):
        await engine._decompression_message("alice", False, 1)
    assert deliveries == [("alice", "normal")]


@pytest.mark.asyncio
async def test_aftermath_caption_and_image_context_belong_to_owner():
    engine = ProactiveEngine()
    engine._affection_level = 9
    engine._affection_levels["alice"] = 0
    deliveries = []

    async def deliver(message, user_id=PRIMARY):
        deliveries.append((user_id, message))

    def discard_task(coro):
        coro.close()
        return MagicMock()

    engine.set_callback(deliver)
    with (
        patch("app.image_gen.build_mission_prompt", return_value="prompt") as build,
        patch(
            "app.proactive.mission._aftermath_captions",
            return_value={"victory": [PRIVATE]},
        ),
        patch("app.proactive.mission.asyncio.create_task", side_effect=discard_task),
    ):
        await engine.trigger_mission_aftermath_image("alice", timer=_timer())
    assert deliveries == [("alice", PRIVATE)]
    assert build.call_args.kwargs["affection_level"] == 0


def test_her_day_context_reads_only_requested_mission(monkeypatch):
    from app import context, her_day

    engine = ProactiveEngine()
    alice = _timer()
    engine._mission_timers["alice"] = alice
    engine._mission_timer = alice
    monkeypatch.setattr(context, "proactive", engine)
    assert her_day.active_mission("bob") is None
    assert her_day.active_mission("alice") == PRIVATE


def test_engine_shutdown_stops_every_users_timer():
    engine = ProactiveEngine()
    alice, bob = _timer(), _timer("Bob mission")
    engine._mission_timers = {"alice": alice, "bob": bob}
    engine._mission_timer = alice
    engine._scheduler = MagicMock()
    engine.stop()
    assert not alice.active
    assert not bob.active


@pytest.mark.asyncio
async def test_reconnect_restores_each_owners_mission_with_primary_already_active(
    monkeypatch,
):
    from app import chat
    from app.memory import MemoryManager
    from .test_session_authority import StoredRedis

    memory = MemoryManager()
    memory._redis = StoredRedis()
    engine = ProactiveEngine()
    primary = _timer("primary patrol")
    engine._mission_timers[PRIMARY] = primary
    engine._mission_timer = primary
    for owner, description in (("alice", PRIVATE), ("bob", "Bob patrol")):
        await memory.save_session(
            chat.session_id(owner),
            SessionState(
                conversation_id=owner,
                mission_description=description,
                mission_interval=15,
            ),
        )
    monkeypatch.setattr(chat, "memory", memory)
    monkeypatch.setattr(chat, "proactive", engine)
    monkeypatch.setattr(
        chat,
        "affection",
        SimpleNamespace(get_state=AsyncMock(return_value=SimpleNamespace(level=4))),
    )
    with patch.object(MissionTimer, "_tick_loop", AsyncMock()):
        try:
            await chat._initialize_session("alice")
            await chat._initialize_session("bob")
            assert engine.get_mission_timer("alice").mission_description == PRIVATE
            assert engine.get_mission_timer("bob").mission_description == "Bob patrol"
            assert engine._mission_timer is primary
        finally:
            for timer in engine._mission_timers.values():
                timer.stop()


@pytest.mark.asyncio
async def test_background_mission_and_mood_remain_scoped(monkeypatch):
    from contextlib import asynccontextmanager
    from app import background
    from app.memory import MemoryManager
    from .test_session_authority import StoredRedis

    memory = MemoryManager()
    memory._redis = StoredRedis()
    engine = ProactiveEngine()
    engine._mission_timers[PRIMARY] = _timer("primary patrol")
    engine._mission_timer = engine._mission_timers[PRIMARY]
    engine.set_last_mood("composed", user_id=PRIMARY)
    snapshot = SessionState(
        conversation_id="alice", turns=[{"role": "user", "content": PRIVATE}]
    )
    await memory.save_session(background.session_id("alice"), snapshot)

    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(execute=AsyncMock())

    monkeypatch.setattr(background, "memory", memory)
    monkeypatch.setattr(background, "proactive", engine)
    monkeypatch.setattr(background, "ws", SimpleNamespace(send_mood=AsyncMock()))
    monkeypatch.setattr(background, "get_conn_autocommit", connection)
    with patch.object(MissionTimer, "_tick_loop", AsyncMock()):
        try:
            assert await background._apply_extracted_state(
                snapshot, "battle_ready", "alice"
            )
            assert engine.get_mission_timer("alice").mission_description == PRIVATE
            assert engine._moods["alice"] == "battle_ready"
            assert engine._last_mood == "composed"
        finally:
            for timer in engine._mission_timers.values():
                timer.stop()
