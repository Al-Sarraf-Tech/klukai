"""Behavioral unit tests for app.chat_handlers._handle_message.

The message pipeline: receive a turn, assemble context, route to the LLM,
stream tokens in order, persist user+assistant turns, fire memory/extraction,
and surface errors. Every test asserts observable behavior (tokens streamed,
turns persisted, branch chosen, background task scheduled) — no no-assert
coverage padding.

All external services (router/ws/memory/affection/proactive/db/background)
are mocked at the module namespaces chat_handlers resolves them from.
Deterministic: datetime frozen where it matters, asyncio.sleep no-op'd,
randomness avoided by driving short-message / nudge branches explicitly.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import LLMConfig, SessionState  # noqa: E402


# ── Stream helper ─────────────────────────────────────────────────────────────


def _make_stream(tokens):
    """Build an async-generator factory mimicking LLMRouter.stream()."""

    async def _stream(system_prompt, messages, config):
        for tok in tokens:
            yield tok

    return _stream


def _fresh_session(turns=None, **kw):
    return SessionState(conversation_id="conv-123", turns=turns or [], **kw)


# ── The big patch harness ─────────────────────────────────────────────────────


@contextmanager
def _patched_pipeline(
    *,
    stream_tokens=("Hello", ", Commander."),
    needs_agent=False,
    game_active=False,
    birthday_block="",
    provider="anthropic",
    aff_level=5,
    nudge=None,
    needs_image_val=False,
    wants_recall_val=False,
    agent_response="Agent says hi.",
):
    """Patch every collaborator _handle_message touches. Yields a namespace
    of the key mocks so each test can assert against them."""

    ns = MagicMock()

    # ── router ──
    router = MagicMock()
    router.needs_agent = AsyncMock(return_value=needs_agent)
    router.is_game_active = AsyncMock(return_value=game_active)
    router.route = AsyncMock(
        return_value=LLMConfig(provider=provider, model="dolphin-24b")
    )
    router.stream = _make_stream(stream_tokens)
    ns.router = router

    # ── ws (ordered call log via a single mock parent) ──
    ws = MagicMock()
    ws.send_token = AsyncMock()
    ws.send_done = AsyncMock()
    ws.send_thinking = AsyncMock()
    ws.send = AsyncMock()
    ws.track_task = MagicMock()
    ns.ws = ws

    # ── memory ──
    async def _add_turn(sid, role, content, state):
        state.turns.append({"role": role, "content": content})
        state.turn_count += 1
        return state

    memory = MagicMock()
    memory.add_turn = AsyncMock(side_effect=_add_turn)
    memory.save_session = AsyncMock()
    memory.recall_for_prompt = AsyncMock(return_value=(["ep1"], {"name": "x"}, ["ex1"]))
    memory.get_memory_nudge = AsyncMock(return_value=nudge)
    memory.get_inside_jokes = AsyncMock(return_value=[])  # inside-jokes feature
    memory.pin_next_recall = AsyncMock()
    ns.memory = memory

    # ── affection ──
    aff_state = MagicMock()
    aff_state.score = aff_level * 100
    aff_state.level = aff_level
    aff_state.first_interaction = None
    affection = MagicMock()
    affection.get_state = AsyncMock(return_value=aff_state)
    ns.affection = affection
    ns.aff_state = aff_state

    # ── proactive ──
    proactive = MagicMock()
    proactive.mark_user_messaged_today = MagicMock()
    proactive.mark_responded = MagicMock()
    proactive.mission_active = False
    proactive._mission_timer = None
    proactive.get_mission_timer = MagicMock(return_value=None)
    proactive.start_mission = MagicMock()
    proactive.stop_mission = MagicMock()
    proactive.record_first = AsyncMock()
    proactive.check_anniversaries = AsyncMock(return_value=[])
    proactive.get_comfort_objects = AsyncMock(return_value=[])
    ns.proactive = proactive

    # ── physical ──
    physical = MagicMock()
    physical.get_state = AsyncMock(return_value=("rested", "feeling sharp"))
    ns.physical = physical

    # ── tributes ──
    tributes = MagicMock()
    tributes.get_crown_jewel = AsyncMock(return_value=None)

    # ── agent loop ──
    agent_result = MagicMock()
    agent_result.response = agent_response
    agent_result.model = "agent-model"
    agent_result.iterations = 2
    agent_result.tools_used = ["web_search"]
    agent = MagicMock()
    agent.run = AsyncMock(return_value=agent_result)
    AgentLoopCls = MagicMock(return_value=agent)
    ns.agent = agent
    ns.AgentLoop = AgentLoopCls

    # ── db autocommit conn ──
    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.execute = AsyncMock()
    get_conn_autocommit = MagicMock(return_value=conn)
    ns.conn = conn

    # ── background tasks (awaitables so create_task accepts them) ──
    async def _noop(*a, **k):
        return None

    background_extraction = AsyncMock(side_effect=_noop)
    background_compaction = AsyncMock(side_effect=_noop)
    background_image_gen = AsyncMock(side_effect=_noop)
    background_recall = AsyncMock(side_effect=_noop)
    do_memory_keep = AsyncMock(side_effect=_noop)
    ns.background_extraction = background_extraction
    ns.background_image_gen = background_image_gen
    ns.background_recall = background_recall
    ns.do_memory_keep = do_memory_keep
    ns.background_compaction = background_compaction

    # ── store_message (returns True = persisted, mirroring helpers.store_message) ──
    store_message = AsyncMock(return_value=True)
    ns.store_message = store_message

    # ── context module (get_last_memory_id) ──
    ctx = MagicMock()
    ctx.get_last_memory_id = MagicMock(return_value=None)
    ns.context = ctx

    needs_image_fn = MagicMock(return_value=needs_image_val)
    ns.needs_image = needs_image_fn

    patches = [
        patch("app.chat_handlers.router", router),
        patch("app.chat_handlers.ws", ws),
        patch("app.chat_handlers.memory", memory),
        patch("app.chat_handlers.affection", affection),
        patch("app.chat_handlers.proactive", proactive),
        patch("app.chat_handlers.context", ctx),
        patch("app.chat_handlers.mcp", MagicMock()),
        patch("app.chat_handlers.AgentLoop", AgentLoopCls),
        patch("app.chat_handlers.get_conn_autocommit", get_conn_autocommit),
        patch("app.chat_handlers._store_message", store_message),
        patch("app.chat_handlers._fix_narration", side_effect=lambda t: t),
        patch("app.chat_handlers._chunk_text", side_effect=lambda t, n=8: [t]),
        patch("app.chat_handlers._wants_recall", return_value=wants_recall_val),
        patch("app.chat_handlers._wants_mission_start", return_value=False),
        patch("app.chat_handlers._wants_mission_cancel", return_value=False),
        patch("app.chat_handlers._parse_interval_minutes", return_value=30),
        patch("app.chat_handlers.assemble_system_prompt", return_value="SYS"),
        patch("app.chat_handlers.needs_image", needs_image_fn),
        patch("app.chat_handlers.background_extraction", background_extraction),
        patch("app.chat_handlers.background_compaction", background_compaction),
        patch("app.chat_handlers.background_image_gen", background_image_gen),
        patch("app.chat_handlers.background_recall", background_recall),
        patch("app.chat_handlers.do_memory_keep", do_memory_keep),
        # deferred imports resolved from their source modules
        patch("app.context.physical", physical),
        patch("app.tributes.get_crown_jewel", tributes.get_crown_jewel),
        patch("app.helpers.detect_squad_address", return_value=None),
        patch("app.helpers.detect_jealousy_trigger", return_value=None),
        patch("app.helpers.wants_dream_inquiry", return_value=False),
        patch("app.rituals.birthday_prompt_block", AsyncMock(return_value=birthday_block)),
        patch("app.image_gen.needs_image", needs_image_fn),
        patch("app.image_gen.detect_squad_members", return_value=[]),
        patch("asyncio.sleep", new=AsyncMock(return_value=None)),
    ]
    started = [p.start() for p in patches]
    try:
        yield ns
    finally:
        for p in patches:
            p.stop()


async def _drain_tasks():
    """Let any asyncio.create_task background work run to completion."""
    await asyncio.sleep(0)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ── Tests: direct streaming path ──────────────────────────────────────────────


class TestDirectStreamingPath:
    @pytest.mark.asyncio
    async def test_empty_content_returns_early(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline() as ns:
            result = await _handle_message("   ", _fresh_session())

        # Nothing should have been routed or persisted, and nothing was said.
        assert result is None
        ns.router.needs_agent.assert_not_called()
        ns.memory.add_turn.assert_not_called()
        ns.ws.send_done.assert_not_called()

    @pytest.mark.asyncio
    async def test_tokens_streamed_in_order(self):
        from app.chat_handlers import _handle_message

        # Tokens chosen so each ends a flush (sentence enders / >20 chars).
        toks = ["First sentence here.", "Second part!", "Third?"]
        with _patched_pipeline(stream_tokens=toks) as ns:
            await _handle_message("Tell me a long story please", _fresh_session())
            await _drain_tasks()

        sent = [c.args[1] for c in ns.ws.send_token.call_args_list]
        # Concatenation of streamed chunks must equal the joined tokens, in order.
        assert "".join(sent) == "".join(toks)
        assert sent[0].startswith("First sentence")

    @pytest.mark.asyncio
    async def test_thinking_then_done_envelope(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline() as ns:
            await _handle_message("Hello there Commander friend", _fresh_session())
            await _drain_tasks()

        ns.ws.send_thinking.assert_awaited()  # "Composing response..."
        ns.ws.send_done.assert_awaited_once()
        done_args = ns.ws.send_done.await_args
        assert done_args.args[2] == "dolphin-24b"  # model_name positional

    @pytest.mark.asyncio
    async def test_user_and_assistant_turns_persisted(self):
        from app.chat_handlers import _handle_message

        session = _fresh_session()
        with _patched_pipeline(stream_tokens=["Acknowledged."]) as ns:
            result = await _handle_message("Long enough to recall properly", session)
            await _drain_tasks()

        roles = [c.args[1] for c in ns.memory.add_turn.call_args_list]
        assert roles == ["user", "assistant"]
        # Assistant content is the reassembled stream, and it is what the
        # caller gets back (the voice path speaks exactly this).
        assert ns.memory.add_turn.call_args_list[-1].args[2] == "Acknowledged."
        assert result == "Acknowledged."

    @pytest.mark.asyncio
    async def test_messages_persisted_to_postgres(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline(stream_tokens=["Reply here now."]) as ns:
            await _handle_message("A meaningful question for you", _fresh_session())
            await _drain_tasks()

        stored_roles = [c.args[1] for c in ns.store_message.call_args_list]
        assert "user" in stored_roles and "assistant" in stored_roles
        # read_receipt sent to the client.
        types = [c.args[1].get("type") for c in ns.ws.send.call_args_list]
        assert "read_receipt" in types

    @pytest.mark.asyncio
    async def test_affection_state_fetched_for_modulation(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline() as ns:
            await _handle_message("How are you feeling today", _fresh_session())
            await _drain_tasks()

        ns.affection.get_state.assert_awaited()

    @pytest.mark.asyncio
    async def test_short_message_skips_recall(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline(stream_tokens=["Hi."]) as ns:
            await _handle_message("hello", _fresh_session())  # <= 20 chars
            await _drain_tasks()

        ns.memory.recall_for_prompt.assert_not_called()
        ns.memory.get_inside_jokes.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_memory_question_still_recalls(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline(stream_tokens=["March."]) as ns:
            await _handle_message("my birthday?", _fresh_session())
            await _drain_tasks()

        ns.memory.recall_for_prompt.assert_awaited_once()
        ns.memory.get_inside_jokes.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_long_message_triggers_recall(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline(stream_tokens=["Sure."]) as ns:
            await _handle_message(
                "Tell me everything about the squad and Mechty", _fresh_session()
            )
            await _drain_tasks()

        ns.memory.recall_for_prompt.assert_awaited_once()
        # affection_level must be threaded so importance re-ranking is not stuck at 0
        kwargs = ns.memory.recall_for_prompt.await_args.kwargs
        assert "affection_level" in kwargs
        assert kwargs["affection_level"] == ns.affection.get_state.return_value.level

    @pytest.mark.asyncio
    async def test_warmup_timer_scheduled_for_any_model(self):
        from app.chat_handlers import _handle_message

        # any provider -> a warmup task is created (then cancelled). With
        # asyncio.sleep no-op'd it never fires send_thinking the 2nd time, but
        # the path is exercised. Assert the first thinking message still sent.
        with _patched_pipeline(provider="anthropic", stream_tokens=["Yo."]) as ns:
            await _handle_message("A normal length question here", _fresh_session())
            await _drain_tasks()

        texts = [c.args[1] for c in ns.ws.send_thinking.await_args_list]
        assert texts[0] == "…"
        assert any("Composing" in text for text in texts)


# ── Tests: agent path ─────────────────────────────────────────────────────────


class TestAgentPath:
    @pytest.mark.asyncio
    async def test_agent_loop_used_when_needs_agent(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline(needs_agent=True, agent_response="Tool result!") as ns:
            await _handle_message("what is the price of bitcoin", _fresh_session())
            await _drain_tasks()

        ns.AgentLoop.assert_called_once()
        ns.agent.run.assert_awaited_once()
        # Router.stream must NOT be the source — direct streaming skipped.
        ns.router.route.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_response_streamed_and_done_with_agent_model(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline(needs_agent=True, agent_response="Done it.") as ns:
            await _handle_message("search the web for prices", _fresh_session())
            await _drain_tasks()

        sent = [c.args[1] for c in ns.ws.send_token.call_args_list]
        assert "".join(sent) == "Done it."  # _chunk_text stub returns whole text
        assert ns.ws.send_done.await_args.args[2] == "agent-model"

    @pytest.mark.asyncio
    async def test_agent_assistant_turn_persisted(self):
        from app.chat_handlers import _handle_message

        session = _fresh_session()
        with _patched_pipeline(needs_agent=True, agent_response="Persisted.") as ns:
            await _handle_message("look up something external", session)
            await _drain_tasks()

        assert ns.memory.add_turn.call_args_list[-1].args[2] == "Persisted."


# ── Tests: mission timer branches ───────────────────────────────────────────────


class TestMissionBranches:
    @pytest.mark.asyncio
    async def test_mission_start_persists_state(self):
        from app.chat_handlers import _handle_message

        session = _fresh_session(turns=[{"role": "user", "content": "scout the sector"}])
        with _patched_pipeline() as ns:
            with patch("app.chat_handlers._wants_mission_start", return_value=True):
                await _handle_message("give me updates every 30 minutes", session)
                await _drain_tasks()

        ns.proactive.start_mission.assert_called_once()
        assert session.mission_interval == 30
        assert session.mission_description  # non-empty
        ns.proactive.record_first.assert_awaited_with("default", "first_mission")

    @pytest.mark.asyncio
    async def test_mission_start_falls_back_to_content_when_no_user_turns(self):
        """No recent user turns -> mission description defaults to the message
        content itself (covers the empty-desc fallback)."""
        from app.chat_handlers import _handle_message

        session = _fresh_session(turns=[])  # nothing to derive desc from
        with _patched_pipeline() as ns:
            with patch("app.chat_handlers._wants_mission_start", return_value=True):
                await _handle_message("ping me every 10 minutes", session)
                await _drain_tasks()

        # Falls back to the literal content.
        assert session.mission_description == "ping me every 10 minutes"

    @pytest.mark.asyncio
    async def test_mission_cancel_clears_state(self):
        from app.chat_handlers import _handle_message

        session = _fresh_session()
        session.mission_description = "old mission"
        session.mission_interval = 15
        with _patched_pipeline() as ns:
            ns.proactive.mission_active = True
            with patch("app.chat_handlers._wants_mission_cancel", return_value=True):
                await _handle_message("stand down", session)
                await _drain_tasks()

        ns.proactive.stop_mission.assert_called_once()
        assert session.mission_description is None
        assert session.mission_interval is None


# ── Tests: save/discard + background dispatch ───────────────────────────────────


class TestBackgroundDispatch:
    @pytest.mark.asyncio
    async def test_save_keyword_marks_memory_kept(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline(stream_tokens=["Saved."]) as ns:
            ns.context.get_last_memory_id.return_value = "mem-99"
            await _handle_message("save that memory please now", _fresh_session())
            await _drain_tasks()

        ns.do_memory_keep.assert_awaited()
        assert ns.do_memory_keep.await_args.args == ("mem-99",)
        assert ns.do_memory_keep.await_args.kwargs.get("kept") is True

    @pytest.mark.asyncio
    async def test_discard_keyword_marks_memory_not_kept(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline(stream_tokens=["Forgotten."]) as ns:
            ns.context.get_last_memory_id.return_value = "mem-12"
            await _handle_message("forget that thing entirely", _fresh_session())
            await _drain_tasks()

        ns.do_memory_keep.assert_awaited()
        assert ns.do_memory_keep.await_args.kwargs.get("kept") is False

    @pytest.mark.asyncio
    async def test_recall_request_schedules_background_recall(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline(stream_tokens=["Remembering."], wants_recall_val=True) as ns:
            await _handle_message("do you remember our first ride", _fresh_session())
            await _drain_tasks()

        ns.background_recall.assert_awaited_once()
        # recall takes priority -> image gen not invoked even if needs_image
        ns.background_image_gen.assert_not_called()

    @pytest.mark.asyncio
    async def test_image_request_schedules_background_image_gen(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline(
            stream_tokens=["Already done."], needs_image_val=True, wants_recall_val=False
        ) as ns:
            await _handle_message("draw yourself by the motorcycle", _fresh_session())
            await _drain_tasks()

        ns.background_image_gen.assert_awaited_once()
        image_tracks = [
            call for call in ns.ws.track_task.call_args_list
            if call.kwargs.get("survive_disconnect") is True
        ]
        assert image_tracks and image_tracks[0].args[0] == "default"

    @pytest.mark.asyncio
    async def test_nontrivial_message_schedules_extraction(self):
        from app.chat_handlers import _handle_message

        with _patched_pipeline(stream_tokens=["A thoughtful reply."]) as ns:
            await _handle_message("I have been thinking about our future", _fresh_session())
            await _drain_tasks()

        ns.background_extraction.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trivial_message_skips_extraction(self):
        from app.chat_handlers import _handle_message

        # "ok" is in TRIVIAL_PATTERNS -> extraction skipped.
        with _patched_pipeline(stream_tokens=["Mm."]) as ns:
            await _handle_message("ok", _fresh_session())
            await _drain_tasks()

        ns.background_extraction.assert_not_called()

    @pytest.mark.asyncio
    async def test_compaction_scheduled_past_threshold(self):
        from app.chat_handlers import _handle_message

        # Pre-load 8 turns; +1 user +1 assistant pushes len(turns) >= 8.
        turns = [{"role": "user", "content": f"t{i}"} for i in range(8)]
        session = _fresh_session(turns=turns)
        with _patched_pipeline(stream_tokens=["Reply."]) as ns:
            await _handle_message("A substantive message goes here", session)
            await _drain_tasks()

        ns.background_compaction.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_extraction_skipped_when_response_is_error(self):
        from app.chat_handlers import _handle_message

        # Stream yields the fallback error string -> recall/image branch skipped.
        with _patched_pipeline(
            stream_tokens=["Communications disrupted, Commander."], wants_recall_val=True
        ) as ns:
            await _handle_message("do you remember the mission", _fresh_session())
            await _drain_tasks()

        ns.background_recall.assert_not_called()


# ── Tests: prompt assembly + nudge ─────────────────────────────────────────────


class TestPromptAssembly:
    @pytest.mark.asyncio
    async def test_memory_nudge_appended_to_system_prompt(self):
        from app.chat_handlers import _handle_message

        captured = {}

        async def _stream(system_prompt, messages, config):
            captured["sys"] = system_prompt
            captured["messages"] = messages
            for t in ["Ok."]:
                yield t

        with _patched_pipeline(nudge="[Memory: recall this]") as ns:
            ns.router.stream = _stream
            with patch("app.chat_handlers.router", ns.router):
                await _handle_message("Tell me a real story now", _fresh_session())
                await _drain_tasks()

        assert "[Memory: recall this]" in captured["sys"]

    async def _captured_prompt(self, content, **pipeline_kw):
        from app.chat_handlers import _handle_message

        captured = {}

        async def _stream(system_prompt, messages, config):
            captured["sys"] = system_prompt
            yield "Ok."

        with _patched_pipeline(**pipeline_kw) as ns:
            ns.router.stream = _stream
            with patch("app.chat_handlers.router", ns.router):
                await _handle_message(content, _fresh_session())
                await _drain_tasks()
        return captured["sys"]

    @pytest.mark.asyncio
    async def test_thread_block_only_when_commander_asks_about_it(self):
        asked = await self._captured_prompt("Can I read the thread? All of it?", aff_level=6)
        assert "THE THREAD" in asked
        assert "[0200 hours" in asked
        unrelated = await self._captured_prompt("How was patrol this afternoon?", aff_level=6)
        assert "THE THREAD" not in unrelated

    @pytest.mark.asyncio
    async def test_gaming_block_only_while_game_active(self):
        gaming = await self._captured_prompt("gg, one more round", aff_level=3, game_active=True)
        assert "GAME IN PROGRESS" in gaming
        assert "Ask if he's winning" in gaming
        idle = await self._captured_prompt("gg, one more round", aff_level=3)
        assert "GAME IN PROGRESS" not in idle

    @pytest.mark.asyncio
    async def test_birthday_block_appended(self):
        prompt = await self._captured_prompt("morning", birthday_block="COMMANDER'S BIRTHDAY: today")
        assert prompt.endswith("COMMANDER'S BIRTHDAY: today")

    @pytest.mark.asyncio
    async def test_context_summary_prepended_as_system_message(self):
        from app.chat_handlers import _handle_message

        captured = {}

        async def _stream(system_prompt, messages, config):
            captured["messages"] = messages
            for t in ["Ok."]:
                yield t

        session = _fresh_session(context_summary="Earlier we discussed dreams")
        with _patched_pipeline() as ns:
            ns.router.stream = _stream
            with patch("app.chat_handlers.router", ns.router):
                await _handle_message("Continue the conversation now", session)
                await _drain_tasks()

        msgs = captured["messages"]
        assert msgs[0]["role"] == "system"
        assert "Earlier we discussed dreams" in msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_cold_session_leads_with_the_persisted_thread(self):
        from app.chat_handlers import _handle_message

        captured = {}

        async def _stream(system_prompt, messages, config):
            captured["messages"] = messages
            yield "Still here."

        earlier = [
            {"role": "user", "content": "the hangar"},
            {"role": "assistant", "content": "Clear."},
            {"role": "user", "content": "hello"},
        ]
        with _patched_pipeline(stream_tokens=["Still here."]) as ns:
            ns.router.stream = _stream
            with (
                patch("app.chat_handlers.router", ns.router),
                patch(
                    "app.chat_handlers.load_recent_thread",
                    new=AsyncMock(return_value=earlier),
                ) as fetch,
            ):
                await _handle_message("hello", _fresh_session())
                await _drain_tasks()

        fetch.assert_awaited_once()
        assert [m["content"] for m in captured["messages"]] == [
            "the hangar",
            "Clear.",
            "hello",
        ]

    @pytest.mark.asyncio
    async def test_warm_session_does_not_reread_the_archive(self):
        from app.chat_handlers import _handle_message

        session = _fresh_session(turns=[{"role": "assistant", "content": "I was waiting."}])
        with _patched_pipeline(stream_tokens=["Go on."]) as ns:
            with patch("app.chat_handlers.load_recent_thread", new=AsyncMock()) as fetch:
                await _handle_message("Tell me what you were waiting for", session)
                await _drain_tasks()

        fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_days_together_computed_from_first_interaction(self):
        from datetime import datetime, timezone, timedelta

        from app.chat_handlers import _handle_message

        captured = {}

        def _capture_prompt(**kw):
            captured.update(kw)
            return "SYS"

        first = datetime.now(timezone.utc) - timedelta(days=30)
        with _patched_pipeline(stream_tokens=["Ok."]) as ns:
            ns.aff_state.first_interaction = first
            with patch("app.chat_handlers.assemble_system_prompt", side_effect=_capture_prompt):
                await _handle_message("A meaningful long enough message", _fresh_session())
                await _drain_tasks()

        # ~30 days elapsed; tolerate clock skew.
        assert captured["days_together"] >= 29

    @pytest.mark.asyncio
    async def test_days_together_defaults_zero_on_bad_first_interaction(self):
        """A first_interaction that can't be subtracted is caught -> days=0."""
        from app.chat_handlers import _handle_message

        captured = {}

        def _capture_prompt(**kw):
            captured.update(kw)
            return "SYS"

        bad = MagicMock()
        bad.tzinfo = None  # forces datetime.now() - MagicMock -> TypeError
        with _patched_pipeline(stream_tokens=["Ok."]) as ns:
            ns.aff_state.first_interaction = bad
            with patch("app.chat_handlers.assemble_system_prompt", side_effect=_capture_prompt):
                await _handle_message("A meaningful long enough message", _fresh_session())
                await _drain_tasks()

        assert captured["days_together"] == 0

    @pytest.mark.asyncio
    async def test_active_mission_description_injected(self):
        from app.chat_handlers import _handle_message

        captured = {}

        def _capture_prompt(**kw):
            captured.update(kw)
            return "SYS"

        with _patched_pipeline(stream_tokens=["Ok."]) as ns:
            ns.proactive.mission_active = True
            ns.proactive._mission_timer = MagicMock()
            ns.proactive._mission_timer.mission_description = "scout sector 7"
            ns.proactive.get_mission_timer.return_value = ns.proactive._mission_timer
            with patch("app.chat_handlers.assemble_system_prompt", side_effect=_capture_prompt):
                await _handle_message("A meaningful long enough message", _fresh_session())
                await _drain_tasks()

        assert captured["mission_description"] == "scout sector 7"

    @pytest.mark.asyncio
    async def test_dream_inquiry_hint_appended(self):
        from app.chat_handlers import _handle_message

        captured = {}

        async def _stream(system_prompt, messages, config):
            captured["sys"] = system_prompt
            for t in ["I don't dream."]:
                yield t

        with _patched_pipeline(stream_tokens=["x"]) as ns:
            ns.router.stream = _stream
            with patch("app.chat_handlers.router", ns.router), patch(
                "app.helpers.wants_dream_inquiry", return_value=True
            ):
                await _handle_message("do you ever have dreams about me", _fresh_session())
                await _drain_tasks()

        assert "DREAM INQUIRY" in captured["sys"]

    @pytest.mark.asyncio
    async def test_image_hint_appended_when_image_needed(self):
        from app.chat_handlers import _handle_message

        captured = {}

        async def _stream(system_prompt, messages, config):
            captured["sys"] = system_prompt
            for t in ["Already done."]:
                yield t

        with _patched_pipeline(stream_tokens=["x"], needs_image_val=True) as ns:
            ns.router.stream = _stream
            with patch("app.chat_handlers.router", ns.router):
                await _handle_message("draw a picture of yourself please", _fresh_session())
                await _drain_tasks()

        assert "IMAGE GENERATION ACTIVE" in captured["sys"]

    @pytest.mark.asyncio
    async def test_trailing_buffer_flushed(self):
        """A token with no sentence-ender under threshold stays in buffer until
        the post-loop flush sends it (covers the final `if buffer` flush)."""
        from app.chat_handlers import _handle_message

        # "tiny" has no .!?\n) and is < 20 chars -> never flushed in-loop.
        with _patched_pipeline(stream_tokens=["tiny"]) as ns:
            await _handle_message("A meaningful long enough message", _fresh_session())
            await _drain_tasks()

        sent = [c.args[1] for c in ns.ws.send_token.call_args_list]
        assert sent == ["tiny"]


class TestWarmupTimer:
    @pytest.mark.asyncio
    async def test_warmup_notify_fires_before_first_token(self):
        """A slow first token lets the warmup task body run first and send the
        'One moment, Commander.' thinking message (covers the inner coroutine).

        The stream yields the event loop (real sleep 0) before its first token,
        and the warmup sleep is patched to resolve immediately, so the warmup
        send_thinking fires before any token cancels the timer."""
        from app.chat_handlers import _handle_message

        real_sleep = asyncio.sleep

        async def _slow_stream(system_prompt, messages, config):
            # Yield control twice so the (instant) warmup task gets to run.
            await real_sleep(0)
            await real_sleep(0)
            yield "Finally a token."

        # Patch chat_handlers' asyncio.sleep so the warmup 8s wait is instant,
        # but the stream above uses the captured real sleep to yield the loop.
        with _patched_pipeline(provider="anthropic") as ns:
            ns.router.stream = _slow_stream
            with patch("app.chat_handlers.router", ns.router), patch(
                "app.chat_handlers.asyncio.sleep", new=AsyncMock(return_value=None)
            ):
                await _handle_message("A normal length question here", _fresh_session())
                await _drain_tasks()

        thinking = [c.args[1] for c in ns.ws.send_thinking.await_args_list]
        assert any("One moment, Commander." in t for t in thinking)

    @pytest.mark.asyncio
    async def test_warmup_timer_cancelled_after_loop_when_pending(self):
        """Empty stream + a warmup sleep that never resolves -> after the (empty)
        loop the timer is still pending and gets cancelled (covers line 285)."""
        from app.chat_handlers import _handle_message

        async def _empty_stream(system_prompt, messages, config):
            return
            yield  # pragma: no cover

        never = asyncio.Event()

        async def _hang(*_a, **_k):
            await never.wait()

        with _patched_pipeline(provider="anthropic") as ns:
            ns.router.stream = _empty_stream
            with patch("app.chat_handlers.router", ns.router), patch(
                "app.chat_handlers.asyncio.sleep", new=_hang
            ):
                await _handle_message("A normal length question here", _fresh_session())

        # The interim line must NOT have fired (cancelled).
        thinking = [c.args[1] for c in ns.ws.send_thinking.await_args_list]
        assert not any("One moment, Commander." in t for t in thinking)


class TestErrorResilience:
    @pytest.mark.asyncio
    async def test_read_at_db_error_is_swallowed(self):
        """A failure setting read_at must not abort the turn — done + stores
        still happen, and the read_receipt is still sent."""
        from app.chat_handlers import _handle_message

        with _patched_pipeline(stream_tokens=["Reply here."]) as ns:
            ns.conn.execute = AsyncMock(side_effect=RuntimeError("db down"))
            await _handle_message("A meaningful long enough message", _fresh_session())
            await _drain_tasks()

        ns.ws.send_done.assert_awaited_once()
        types = [c.args[1].get("type") for c in ns.ws.send.call_args_list]
        assert "read_receipt" in types

    @pytest.mark.asyncio
    async def test_extraction_crash_is_caught(self):
        """If background_extraction raises, the _safe_extraction wrapper logs and
        swallows it — _handle_message itself never raises."""
        from app.chat_handlers import _handle_message

        with _patched_pipeline(stream_tokens=["A real reply here."]) as ns:
            ns.background_extraction.side_effect = RuntimeError("extract boom")
            # Should complete without propagating.
            await _handle_message("Tell me about your day in detail", _fresh_session())
            await _drain_tasks()

        ns.background_extraction.assert_awaited_once()


# ── Tests: stream-failure + persistence-before-streaming (2026-06-11) ────────


class TestStreamFailureHandling:
    @pytest.mark.asyncio
    async def test_user_message_persisted_before_streaming(self):
        """The user turn must hit PostgreSQL BEFORE streaming starts, so a
        mid-stream crash can never lose the Commander's message."""
        from app.chat_handlers import _handle_message

        with _patched_pipeline() as ns:
            seen = {}

            async def _stream(system_prompt, messages, config):
                seen["user_stored_before_stream"] = any(
                    c.args[1] == "user" for c in ns.store_message.call_args_list
                )
                yield "Reply."

            ns.router.stream = _stream
            await _handle_message("A meaningful question for you", _fresh_session())
            await _drain_tasks()

        assert seen["user_stored_before_stream"] is True
        # And it is not stored twice.
        user_stores = [c for c in ns.store_message.call_args_list if c.args[1] == "user"]
        assert len(user_stores) == 1

    @pytest.mark.asyncio
    async def test_midstream_failure_surfaces_error_and_keeps_message(self):
        """LLMStreamFailed mid-response must not propagate (it would kill the
        WS): the client gets an error event, the user message stays stored,
        and no background extraction runs on the truncated reply."""
        from app.chat_handlers import _handle_message
        from app.llm_router import LLMStreamFailed

        with _patched_pipeline() as ns:
            async def _stream(system_prompt, messages, config):
                yield "Partial sentence."
                raise LLMStreamFailed("upstream died")

            ns.router.stream = _stream
            await _handle_message("Tell me something long enough", _fresh_session())
            await _drain_tasks()

        # User message persisted (before streaming) despite the failure.
        stored_roles = [c.args[1] for c in ns.store_message.call_args_list]
        assert "user" in stored_roles
        # Client got an explicit error event…
        types = [c.args[1].get("type") for c in ns.ws.send.call_args_list]
        assert "error" in types
        # …and the stream still finalized for the UI.
        ns.ws.send_done.assert_awaited_once()
        # Truncated reply: no extraction/compaction follow-ups.
        ns.background_extraction.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_store_failure_sends_warning_event(self):
        """If the user message could not be persisted, the Commander is warned
        instead of the failure being silently swallowed."""
        from app.chat_handlers import _handle_message

        with _patched_pipeline(stream_tokens=["Understood, Commander."]) as ns:
            ns.store_message.return_value = False
            await _handle_message("A meaningful question for you", _fresh_session())
            await _drain_tasks()

        types = [c.args[1].get("type") for c in ns.ws.send.call_args_list]
        assert "warning" in types


# ── Tests: turns she could not answer (dominus off / stream died) ──────────────


def _prior_exchange():
    return [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]


class TestNoAnswerTurns:
    """When her core produces nothing — the gateway is down before any token,
    the stream dies, or the agent loop gives up — the Commander's message is
    kept in PostgreSQL, but the non-answer is not recorded as hers, not mined
    for facts/mood, and the orphan user turn is taken back out of the live
    window so retries during a power cycle cannot evict real history."""

    @pytest.mark.asyncio
    async def test_route_runtime_error_degrades_in_band(self):
        """dominus-nobara powered off: route() raises RuntimeError. That is
        normal, so no traceback path / "something glitched" error frame — the
        sentinel is streamed as the reply and the turn finalises cleanly."""
        from app.chat_handlers import _handle_message
        from app.llm_router import FAILURE_SENTINEL

        session = _fresh_session(turns=_prior_exchange(), turn_count=2)
        with _patched_pipeline() as ns:
            ns.router.route = AsyncMock(side_effect=RuntimeError("No local LLM backend available"))
            result = await _handle_message("Are you there, Klukai?", session)
            await _drain_tasks()

        assert result is None
        ns.ws.send_token.assert_awaited_once_with("default", FAILURE_SENTINEL)
        ns.ws.send_done.assert_awaited_once()
        assert ns.ws.send_done.await_args.args[2] == "offline"
        types = [c.args[1].get("type") for c in ns.ws.send.call_args_list]
        assert "error" not in types
        # SACRED: his message reached PostgreSQL; nothing of hers was invented.
        assert [c.args[1] for c in ns.store_message.call_args_list] == ["user"]
        # Live window: back to the last real exchange, count consistent.
        assert session.turns == _prior_exchange()
        assert session.turn_count == 2
        ns.memory.save_session.assert_awaited()
        ns.background_extraction.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("empty", ["", " \n\t"])
    @pytest.mark.parametrize("agent", [False, True])
    async def test_empty_reply_is_not_recorded_or_mined(self, empty, agent):
        from app.chat_handlers import _handle_message

        session = _fresh_session(turns=_prior_exchange(), turn_count=2)
        with _patched_pipeline(
            stream_tokens=[empty], needs_agent=agent, agent_response=empty
        ) as ns:
            result = await _handle_message("Tell me about our last mission", session)
            await _drain_tasks()

        assert result is None
        assert [c.args[1] for c in ns.store_message.call_args_list] == ["user"]
        assert session.turns == _prior_exchange()
        assert session.turn_count == 2
        ns.background_extraction.assert_not_called()
        assert any(c.args[1].strip() for c in ns.ws.send_token.call_args_list)
        ns.ws.send_done.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sentinel_reply_is_not_recorded_or_mined(self):
        """The pre-token failure sentinel used to be add_turn'd, stored,
        read-receipted and fed to extraction (mood reset to composed, streak
        credited, sentinel in long-term memory). None of that now."""
        from app.chat_handlers import _handle_message
        from app.llm_router import FAILURE_SENTINEL

        session = _fresh_session(turns=_prior_exchange(), turn_count=2)
        with _patched_pipeline(stream_tokens=[FAILURE_SENTINEL], wants_recall_val=True) as ns:
            result = await _handle_message("do you remember the mission", session)
            await _drain_tasks()

        assert result is None
        # The UI still saw the sentinel bubble and the done frame.
        ns.ws.send_token.assert_awaited_once_with("default", FAILURE_SENTINEL)
        ns.ws.send_done.assert_awaited_once()
        # Not hers: only the user turn was added/stored, no read receipt.
        assert [c.args[1] for c in ns.memory.add_turn.call_args_list] == ["user"]
        assert [c.args[1] for c in ns.store_message.call_args_list] == ["user"]
        types = [c.args[1].get("type") for c in ns.ws.send.call_args_list]
        assert "read_receipt" not in types
        # No follow-ups of any kind.
        ns.background_recall.assert_not_called()
        ns.background_extraction.assert_not_called()
        ns.background_compaction.assert_not_called()
        # Orphan user turn dropped from the live window.
        assert session.turns == _prior_exchange()
        assert session.turn_count == 2

    @pytest.mark.asyncio
    async def test_midstream_failure_does_not_persist_partial_reply(self):
        """A truncated answer is not hers either: no assistant add_turn/store,
        and the user turn is dropped so his re-send has clean context."""
        from app.chat_handlers import _handle_message
        from app.llm_router import LLMStreamFailed

        session = _fresh_session(turns=_prior_exchange(), turn_count=2)
        with _patched_pipeline() as ns:
            async def _stream(system_prompt, messages, config):
                yield "Partial sentence."
                raise LLMStreamFailed("upstream died")

            ns.router.stream = _stream
            result = await _handle_message("Tell me something long enough", session)
            await _drain_tasks()

        assert result is None
        assert [c.args[1] for c in ns.memory.add_turn.call_args_list] == ["user"]
        assert [c.args[1] for c in ns.store_message.call_args_list] == ["user"]
        assert session.turns == _prior_exchange()
        assert session.turn_count == 2

    @pytest.mark.asyncio
    async def test_midstream_failure_drops_unflushed_buffer(self):
        """The unflushed tail is NOT sent after the error frame — a token
        following an error opens a stray second bubble in the client. `done`
        still goes out so clients unlock."""
        from app.chat_handlers import _handle_message
        from app.llm_router import LLMStreamFailed

        with _patched_pipeline() as ns:
            async def _stream(system_prompt, messages, config):
                yield "Partial"  # no sentence ender, under the flush threshold
                raise LLMStreamFailed("upstream died")

            ns.router.stream = _stream
            await _handle_message("Tell me something long enough", _fresh_session())
            await _drain_tasks()

        ns.ws.send_token.assert_not_called()
        types = [c.args[1].get("type") for c in ns.ws.send.call_args_list]
        assert "error" in types
        ns.ws.send_done.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_agent_failure_line_is_not_recorded(self):
        """The agent loop's own give-up line shares the sentinel's opening
        words; it is a non-answer too."""
        from app.chat_handlers import _handle_message

        session = _fresh_session()
        with _patched_pipeline(
            needs_agent=True,
            agent_response="Communications disrupted, Commander. I'll respond with what I have.",
        ) as ns:
            result = await _handle_message("what is the price of bitcoin", session)
            await _drain_tasks()

        assert result is None
        assert [c.args[1] for c in ns.memory.add_turn.call_args_list] == ["user"]
        assert [c.args[1] for c in ns.store_message.call_args_list] == ["user"]
        ns.background_extraction.assert_not_called()
        assert session.turns == []

    @pytest.mark.asyncio
    async def test_drop_leaves_a_window_not_ending_in_a_user_turn_alone(self):
        """Defensive: if the live window's last turn is not his (another
        writer got there first), nothing is popped and nothing re-saved."""
        from app.chat_handlers import _drop_unanswered_turn

        session = _fresh_session(turns=_prior_exchange(), turn_count=2)
        with _patched_pipeline() as ns:
            await _drop_unanswered_turn("sid", session)

        assert session.turns == _prior_exchange()
        assert session.turn_count == 2
        ns.memory.save_session.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# _hours_since_last_exchange — presence gap
# ─────────────────────────────────────────────────────────────────────────


class TestHoursSinceLastExchange:
    """The presence block used to derive the gap from `last_interaction_date`,
    a DATE column, so every absence rounded to a multiple of 24 hours and
    disagreed with the return-greeting (which reads real timestamps)."""

    @staticmethod
    def _conn_returning(row):
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        result = MagicMock()
        result.fetchone = AsyncMock(return_value=row)
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=result)

        @asynccontextmanager
        async def _cm():
            yield conn

        return _cm, conn

    @pytest.mark.asyncio
    async def test_reports_a_real_sub_day_gap(self):
        from datetime import datetime, timedelta, timezone

        from app.chat_handlers import _hours_since_last_exchange

        last = datetime.now(timezone.utc) - timedelta(hours=10)
        cm, _ = self._conn_returning((last,))
        with patch("app.db.get_conn", cm):
            gap = await _hours_since_last_exchange("claude")

        assert gap is not None
        assert 9.9 < gap < 10.1  # not snapped to 24

    @pytest.mark.asyncio
    async def test_naive_timestamp_is_treated_as_utc(self):
        from datetime import datetime, timedelta, timezone

        from app.chat_handlers import _hours_since_last_exchange

        last = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(tzinfo=None)
        cm, _ = self._conn_returning((last,))
        with patch("app.db.get_conn", cm):
            gap = await _hours_since_last_exchange("claude")

        assert 2.9 < gap < 3.1

    @pytest.mark.asyncio
    async def test_excludes_her_own_check_ins(self):
        from datetime import datetime, timezone

        from app.chat_handlers import _hours_since_last_exchange

        cm, conn = self._conn_returning((datetime.now(timezone.utc),))
        with patch("app.db.get_conn", cm):
            await _hours_since_last_exchange("claude")

        sql = conn.execute.await_args.args[0]
        assert "proactive" in sql

    @pytest.mark.asyncio
    async def test_binds_the_grace_window_as_a_parameter(self):
        """A placeholder inside a quoted interval literal does not bind under
        psycopg3 — see tests/test_sql_bind_guard.py."""
        from datetime import datetime, timezone

        from app.chat_handlers import _PRESENCE_IGNORE_SECONDS, _hours_since_last_exchange

        cm, conn = self._conn_returning((datetime.now(timezone.utc),))
        with patch("app.db.get_conn", cm):
            await _hours_since_last_exchange("claude")

        sql, params = conn.execute.await_args.args[0], conn.execute.await_args.args[1]
        assert "make_interval" in sql
        assert _PRESENCE_IGNORE_SECONDS in params

    @pytest.mark.asyncio
    async def test_no_history_returns_none_so_caller_can_fall_back(self):
        from app.chat_handlers import _hours_since_last_exchange

        cm, _ = self._conn_returning((None,))
        with patch("app.db.get_conn", cm):
            assert await _hours_since_last_exchange("claude") is None

    @pytest.mark.asyncio
    async def test_empty_row_returns_none(self):
        from app.chat_handlers import _hours_since_last_exchange

        cm, _ = self._conn_returning(None)
        with patch("app.db.get_conn", cm):
            assert await _hours_since_last_exchange("claude") is None

    @pytest.mark.asyncio
    async def test_db_failure_is_fail_soft(self):
        from app.chat_handlers import _hours_since_last_exchange

        with patch("app.db.get_conn", side_effect=RuntimeError("pool down")):
            assert await _hours_since_last_exchange("claude") is None

    @pytest.mark.asyncio
    async def test_future_timestamp_clamps_to_zero(self):
        from datetime import datetime, timedelta, timezone

        from app.chat_handlers import _hours_since_last_exchange

        cm, _ = self._conn_returning(
            (datetime.now(timezone.utc) + timedelta(hours=2),)
        )
        with patch("app.db.get_conn", cm):
            assert await _hours_since_last_exchange("claude") == 0.0


class TestContextualPictureRequests:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content,turns,expected",
        [
            (
                "Show us sitting together on the balcony at sunset",
                [],
                "Show us sitting together on the balcony at sunset",
            ),
            (
                "show me",
                [
                    {
                        "role": "user",
                        "content": "We are sitting together on the balcony at sunset",
                    },
                    {"role": "assistant", "content": "The breeze is warm."},
                ],
                "show me\n\nScene: We are sitting together on the balcony at sunset",
            ),
            ("show me", [], None),
            (
                "show me",
                [
                    {
                        "role": "assistant",
                        "content": "We are sitting together on the balcony at sunset",
                    }
                ],
                None,
            ),
            (
                "show me",
                [
                    {
                        "role": "user",
                        "content": "We are sitting together on the balcony at sunset",
                    },
                    {"role": "user", "content": "Explain the server logs"},
                ],
                None,
            ),
            (
                "show me",
                [{"role": "user", "content": "We are debugging code in the office"}],
                None,
            ),
            (
                "show me",
                [
                    {
                        "role": "user",
                        "content": "We are sitting together on the balcony but do not take a picture",
                    }
                ],
                None,
            ),
            ("We are sitting together on the balcony at sunset", [], None),
            ("Can you show us sitting together on the balcony if I ask?", [], None),
        ],
    )
    async def test_picture_decision_and_render_scene_are_consistent(
        self, content, turns, expected
    ):
        from app.chat_handlers import _handle_message
        from app.image_prompts import needs_image

        session = _fresh_session(turns=turns.copy())
        prompts = []

        async def stream(system_prompt, messages, config):
            prompts.append(system_prompt)
            yield "All right."

        with _patched_pipeline() as ns:
            ns.router.stream = stream
            with patch("app.chat_handlers.needs_image", needs_image):
                await _handle_message(content, session)
                await _drain_tasks()

        assert ("IMAGE GENERATION ACTIVE" in prompts[0]) is (expected is not None)
        assert any(
            t["role"] == "user" and t["content"] == content for t in session.turns
        )
        if expected is None:
            ns.background_image_gen.assert_not_called()
        else:
            ns.background_image_gen.assert_awaited_once()
            assert ns.background_image_gen.await_args.args[0] == expected
            assert (
                ns.background_image_gen.await_args.kwargs["recent_turns"][-2]["content"]
                == content
            )
