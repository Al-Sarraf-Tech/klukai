"""Behavioral unit tests for app.background — the async task dispatchers.

Every test asserts REAL behavior: that a downstream call happened (or was
skipped), that a threshold was honored, that a WS frame was sent, that an
exception was swallowed gracefully. No no-assertion coverage padding.

All I/O is mocked: the DB pool, Redis, Qdrant, the LLM router, and
``asyncio.sleep`` (so the 3s/2s/1s real delays never fire). Scheduler-style
loops are driven exactly one iteration; the sleep patch makes time
deterministic with zero wall-clock cost.

background.py imports its collaborators (``affection``, ``memory``, ``ws``,
``proactive``, ``context``, ``memory_archive``, ``extract_facts`` …) as
module-level names, so we patch them at ``app.background.<name>``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.background as bg
from app.models import SessionState


# ── Helpers ──────────────────────────────────────────────────────────────────


def _session(turn_count: int = 1, *, mood: str = "composed",
             context_summary: str | None = None,
             turns: list | None = None) -> SessionState:
    if turns is None:
        turns = [{"role": "user", "content": f"Turn {i}"} for i in range(turn_count)]
    return SessionState(
        conversation_id="conv-1",
        turns=turns,
        context_summary=context_summary,
        mood=mood,
        turn_count=turn_count,
    )


def _aff_state(level: int = 5):
    return SimpleNamespace(level=level, score=level * 100)


def _aff_change(*, new_score=510, new_level=5, new_level_name="Trusted",
                delta=10, level_changed=False, level_direction=""):
    return SimpleNamespace(
        new_score=new_score, new_level=new_level, new_level_name=new_level_name,
        delta=delta, level_changed=level_changed, level_direction=level_direction,
    )


def _base_extract_result(**overrides) -> dict:
    """A minimal extract_facts() return, overridable per test."""
    result = {
        "facts": [],
        "mood": "composed",
        "topics": [],
        "should_remember": False,
        "interaction": {"type": "neutral", "intensity": 5},
        "commander_details": {},
        "gift_item": None,
    }
    result.update(overrides)
    return result


@pytest.fixture
def bg_mocks(monkeypatch):
    """Patch every collaborator background.py reaches into.

    Returns a namespace of the mocks so each test asserts the calls it cares
    about. asyncio.sleep is neutralized so the real 1s/2s/3s delays vanish.
    """
    sleep = AsyncMock()
    monkeypatch.setattr(bg.asyncio, "sleep", sleep)

    affection = MagicMock()
    affection.get_state = AsyncMock(return_value=_aff_state(5))
    affection.apply_classification = AsyncMock(return_value=_aff_change())

    memory = MagicMock()
    memory.set_relationship_fact = AsyncMock()
    memory.save_session = AsyncMock()
    memory.store_exchange = AsyncMock()
    memory.store_episode = AsyncMock()
    memory.record_milestone = AsyncMock(return_value=False)
    memory.get_session = AsyncMock(return_value=_session(2))
    memory.recall_fact = AsyncMock(return_value=None)  # no wardrobe outfit by default

    ws = MagicMock()
    ws.send = AsyncMock()
    ws.send_mood = AsyncMock()
    ws.send_proactive = AsyncMock()
    ws.send_thinking = AsyncMock()
    ws.send_affection = AsyncMock()
    ws.send_affection_level_change = AsyncMock()
    ws.send_heartbeat_spike = AsyncMock()

    proactive = MagicMock()
    proactive.store_gift = AsyncMock()
    proactive.record_first = AsyncMock()
    proactive.set_last_mood = MagicMock()
    proactive.set_affection_level = MagicMock()
    proactive.start_mission = MagicMock()
    proactive.mission_active = False
    proactive._last_mood = "composed"

    context_mod = MagicMock()
    context_mod.COMPACT_THRESHOLD = 8
    context_mod.COMPACT_KEEP_RAW = 4
    context_mod.get_last_memory_id = MagicMock(return_value=None)
    context_mod.set_last_memory_id = MagicMock()

    memory_archive = MagicMock()
    memory_archive.update_curation = AsyncMock()
    memory_archive.save_image = AsyncMock(return_value="mem-123")
    memory_archive.recall_memory = AsyncMock(return_value=None)
    memory_archive.get_image_bytes = AsyncMock(return_value=None)
    memory_archive.update_kept = AsyncMock(return_value=True)

    extract_facts = AsyncMock(return_value=_base_extract_result())

    monkeypatch.setattr(bg, "affection", affection)
    monkeypatch.setattr(bg, "memory", memory)
    monkeypatch.setattr(bg, "ws", ws)
    monkeypatch.setattr(bg, "proactive", proactive)
    monkeypatch.setattr(bg, "context", context_mod)
    monkeypatch.setattr(bg, "memory_archive", memory_archive)
    monkeypatch.setattr(bg, "extract_facts", extract_facts)
    # session_id is imported as a name; keep it deterministic
    monkeypatch.setattr(bg, "session_id", lambda uid: f"session:{uid}")
    # load_personality returns affection config used in level-transition branch
    monkeypatch.setattr(bg, "load_personality", MagicMock(return_value={"affection": {}}))

    return SimpleNamespace(
        sleep=sleep, affection=affection, memory=memory, ws=ws,
        proactive=proactive, context=context_mod, memory_archive=memory_archive,
        extract_facts=extract_facts,
    )


# ── background_extraction: the timing contract ────────────────────────────────


class TestExtractionTiming:
    async def test_sleeps_3s_before_doing_work(self, bg_mocks):
        """Extraction is delayed 3s so it doesn't compete with the stream."""
        await bg.background_extraction("hi", "hello", _session(1), user_id="u1")
        # First sleep call is the deliberate 3s stream-yield delay.
        assert bg_mocks.sleep.await_args_list[0].args == (3,)

    async def test_passes_affection_level_into_extractor(self, bg_mocks):
        """The cached affection level is forwarded to extract_facts."""
        bg_mocks.affection.get_state.return_value = _aff_state(7)
        await bg.background_extraction("u msg", "a msg", _session(1), user_id="u1")
        kwargs = bg_mocks.extract_facts.await_args.kwargs
        assert kwargs["affection_level"] == 7
        assert kwargs["image_generated"] is False


# ── background_extraction: fact / detail / gift persistence ───────────────────


class TestExtractionPersistence:
    async def test_stores_extracted_facts(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            facts=[{"key": "favorite_color", "value": "red"}]
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.memory.set_relationship_fact.assert_any_await(
            "favorite_color", "red", user_id="u1"
        )

    async def test_stores_commander_details_with_prefix(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            commander_details={"rank": "Major", "blank": ""}
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.memory.set_relationship_fact.assert_any_await(
            "commander_rank", "Major", user_id="u1"
        )
        # Empty values are skipped — never stored.
        for call in bg_mocks.memory.set_relationship_fact.await_args_list:
            assert call.args[0] != "commander_blank"

    async def test_stores_gift_and_records_first_gift(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(gift_item="teddy bear")
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.proactive.store_gift.assert_awaited_once_with("u1", "teddy bear")
        bg_mocks.proactive.record_first.assert_any_await("u1", "first_gift")

    async def test_single_char_gift_ignored(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(gift_item="x")
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.proactive.store_gift.assert_not_awaited()


# ── background_extraction: mood + contagion + persistence ─────────────────────


class TestExtractionMood:
    async def test_mood_saved_and_broadcast(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(mood="affectionate")
        session = _session(1)
        await bg.background_extraction("m", "r", session, user_id="u1")
        assert session.mood == "affectionate"
        bg_mocks.ws.send_mood.assert_awaited_once_with("u1", "affectionate")
        bg_mocks.proactive.set_last_mood.assert_called_once_with("affectionate")

    async def test_mood_contagion_nudges_toward_sentiment(self, bg_mocks):
        """When interaction maps to a sentiment, nudge_mood adjusts the mood."""
        bg_mocks.extract_facts.return_value = _base_extract_result(
            mood="composed", interaction={"type": "affection", "intensity": 8}
        )
        session = _session(1)
        with patch("app.character_behaviors.interaction_to_sentiment",
                   return_value="positive"), \
             patch("app.character_behaviors.nudge_mood",
                   return_value="warm") as nudge:
            await bg.background_extraction("m", "r", session, user_id="u1")
        nudge.assert_called_once_with("composed", "positive")
        assert session.mood == "warm"

    async def test_persists_mood_to_postgres(self, bg_mocks):
        """Mood is upserted into companion_persistent_state via autocommit conn."""
        conn = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        with patch.object(bg, "get_conn_autocommit", return_value=cm):
            await bg.background_extraction("m", "r", _session(1), user_id="u1")
        conn.execute.assert_awaited()
        sql = conn.execute.await_args.args[0]
        assert "companion_persistent_state" in sql


# ── background_extraction: anniversary / physical / mission ───────────────────


class TestExtractionMilestones:
    async def test_first_message_records_milestone(self, bg_mocks):
        await bg.background_extraction("m", "r", _session(turn_count=1), user_id="u1")
        bg_mocks.proactive.record_first.assert_any_await("u1", "first_message")
        bg_mocks.memory.record_milestone.assert_any_await("first_message", user_id="u1")

    async def test_long_conversation_triggers_physical_relax(self, bg_mocks):
        """At turn_count multiples of 10, physical.on_long_conversation fires."""
        phys = MagicMock()
        phys.on_long_conversation = AsyncMock()
        with patch("app.context.physical", phys):
            await bg.background_extraction("m", "r", _session(turn_count=10), user_id="u1")
        phys.on_long_conversation.assert_awaited_once_with("u1")

    async def test_battle_ready_mood_auto_starts_mission(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(mood="battle_ready")
        bg_mocks.proactive.mission_active = False
        session = _session(turns=[{"role": "user", "content": "Engage hostiles now"}],
                           turn_count=3)
        await bg.background_extraction("m", "r", session, user_id="u1")
        bg_mocks.proactive.start_mission.assert_called_once()
        assert session.mission_interval == 30
        assert session.mission_description

    async def test_vigilant_starts_mission_only_after_battle_ready(self, bg_mocks):
        """vigilant joins mission_moods only when prev mood was battle_ready."""
        bg_mocks.extract_facts.return_value = _base_extract_result(mood="vigilant")
        bg_mocks.proactive._last_mood = "battle_ready"
        bg_mocks.proactive.mission_active = False
        await bg.background_extraction("m", "r", _session(2), user_id="u1")
        bg_mocks.proactive.start_mission.assert_called_once()

    async def test_vigilant_no_mission_when_prev_mood_calm(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(mood="vigilant")
        bg_mocks.proactive._last_mood = "composed"
        bg_mocks.proactive.mission_active = False
        await bg.background_extraction("m", "r", _session(2), user_id="u1")
        bg_mocks.proactive.start_mission.assert_not_called()


# ── background_extraction: affection + heartbeat ──────────────────────────────


class TestExtractionAffection:
    async def test_applies_classification_and_syncs_proactive(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            interaction={"type": "affection", "intensity": 6}
        )
        bg_mocks.affection.apply_classification.return_value = _aff_change(new_level=6)
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.affection.apply_classification.assert_awaited_once_with(
            "affection", 6, "u1"
        )
        bg_mocks.proactive.set_affection_level.assert_called_once_with(6)
        bg_mocks.ws.send_affection.assert_awaited_once()

    async def test_intensity_clamped_into_1_10(self, bg_mocks):
        """An out-of-range intensity is clamped before apply_classification."""
        bg_mocks.extract_facts.return_value = _base_extract_result(
            interaction={"type": "praise", "intensity": 99}
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        assert bg_mocks.affection.apply_classification.await_args.args[1] == 10

    async def test_string_interaction_coerced_to_dict(self, bg_mocks):
        """interaction given as a bare string is normalized to type+intensity."""
        bg_mocks.extract_facts.return_value = _base_extract_result(interaction="teasing")
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        args = bg_mocks.affection.apply_classification.await_args.args
        assert args[0] == "teasing" and args[1] == 5

    async def test_non_dict_non_str_interaction_defaults_neutral(self, bg_mocks):
        """An interaction that is neither dict nor str falls back to neutral/5."""
        bg_mocks.extract_facts.return_value = _base_extract_result(interaction=42)
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        args = bg_mocks.affection.apply_classification.await_args.args
        assert args[0] == "neutral" and args[1] == 5

    async def test_repeat_level_up_sends_short_message_not_scene(self, bg_mocks):
        """A repeat level-up (milestone already recorded) sends only the short line."""
        bg_mocks.extract_facts.return_value = _base_extract_result(
            interaction={"type": "affection", "intensity": 6}
        )
        bg_mocks.affection.apply_classification.return_value = _aff_change(
            new_level=4, level_changed=True, level_direction="up"
        )
        bg_mocks.memory.record_milestone.return_value = False  # already seen
        with patch.object(bg, "load_personality", return_value={
            "affection": {
                "milestone_scenes": {4: ["scene line"]},
                "level_up_messages": {4: "Welcome back to this place."},
            }
        }):
            await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.ws.send_proactive.assert_any_await("u1", "Welcome back to this place.")
        # The full milestone scene line is NOT delivered on a repeat.
        scene = [c for c in bg_mocks.ws.send_proactive.await_args_list
                 if c.args[1] == "scene line"]
        assert not scene

    async def test_heartbeat_spike_on_high_intensity_passionate(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            mood="passionate", interaction={"type": "affection", "intensity": 8}
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.ws.send_heartbeat_spike.assert_awaited_once_with("u1", 165, "passionate")

    async def test_no_heartbeat_spike_below_intensity_threshold(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            mood="passionate", interaction={"type": "affection", "intensity": 4}
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.ws.send_heartbeat_spike.assert_not_awaited()

    async def test_level_up_milestone_scene_delivered_when_new(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            interaction={"type": "affection", "intensity": 6}
        )
        bg_mocks.affection.apply_classification.return_value = _aff_change(
            new_level=4, level_changed=True, level_direction="up"
        )
        # First time at this milestone → record_milestone returns True
        bg_mocks.memory.record_milestone.return_value = True
        bg_mocks.load_personality = None
        with patch.object(bg, "load_personality", return_value={
            "affection": {"milestone_scenes": {4: ["You've earned this.", "I see you."]}}
        }):
            await bg.background_extraction("m", "r", _session(1), user_id="u1")
        # Two scene lines proactively sent.
        scene_calls = [c for c in bg_mocks.ws.send_proactive.await_args_list
                       if c.args[1] in ("You've earned this.", "I see you.")]
        assert len(scene_calls) == 2

    async def test_level_down_message_delivered(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            interaction={"type": "insult", "intensity": 6}
        )
        bg_mocks.affection.apply_classification.return_value = _aff_change(
            new_level=2, level_changed=True, level_direction="down"
        )
        with patch.object(bg, "load_personality", return_value={
            "affection": {"level_down_messages": {2: "...I expected better."}}
        }):
            await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.ws.send_proactive.assert_any_await("u1", "...I expected better.")


# ── background_extraction: exchange + episode storage ─────────────────────────


class TestExtractionStorage:
    async def test_stores_exchange_with_importance(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            should_remember=True, topics=["patrol"]
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        kwargs = bg_mocks.memory.store_exchange.await_args.kwargs
        assert kwargs["importance"] == 0.7  # should_remember=True
        assert kwargs["topics"] == ["patrol"]
        assert kwargs["user_id"] == "u1"

    async def test_low_importance_when_not_memorable(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(should_remember=False)
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        assert bg_mocks.memory.store_exchange.await_args.kwargs["importance"] == 0.4

    async def test_episode_created_every_10_turns(self, bg_mocks):
        with patch.object(bg, "create_episode_summary", new=AsyncMock(
                return_value="They discussed the upcoming op.")):
            await bg.background_extraction("m", "r", _session(turn_count=10), user_id="u1")
        bg_mocks.memory.store_episode.assert_awaited_once()
        assert bg_mocks.memory.store_episode.await_args.kwargs["summary"]

    async def test_no_episode_off_10_turn_boundary(self, bg_mocks):
        with patch.object(bg, "create_episode_summary", new=AsyncMock(
                return_value="summary")) as ces:
            await bg.background_extraction("m", "r", _session(turn_count=7), user_id="u1")
        ces.assert_not_awaited()
        bg_mocks.memory.store_episode.assert_not_awaited()


# ── background_extraction: curation + resilience ──────────────────────────────


class TestExtractionCurationAndResilience:
    async def test_curation_applied_when_image_generated(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            memory_curation={"category": "battle", "annotation": "A fierce moment."}
        )
        await bg.background_extraction(
            "m", "r", _session(1), user_id="u1",
            image_generated=True, memory_id="mem-xyz",
        )
        bg_mocks.memory_archive.update_curation.assert_awaited_once_with(
            "mem-xyz", {"category": "battle", "annotation": "A fierce moment."}, 5
        )

    async def test_curation_resolves_memory_id_from_context_fallback(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            memory_curation={"category": "candid"}
        )
        bg_mocks.context.get_last_memory_id.return_value = "ctx-mem"
        await bg.background_extraction(
            "m", "r", _session(1), user_id="u1", image_generated=True,
        )
        bg_mocks.context.get_last_memory_id.assert_called_once_with("u1")
        assert bg_mocks.memory_archive.update_curation.await_args.args[0] == "ctx-mem"

    async def test_top_level_exception_swallowed(self, bg_mocks):
        """A failure in extract_facts must NOT propagate (fire-and-forget task)."""
        bg_mocks.extract_facts.side_effect = RuntimeError("LLM down")
        # Must not raise.
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.memory.store_exchange.assert_not_awaited()

    async def test_affection_failure_isolated_from_exchange_storage(self, bg_mocks):
        """A crash in affection handling must not stop the exchange from storing."""
        bg_mocks.affection.apply_classification.side_effect = RuntimeError("aff boom")
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        # Affection block failed, yet the exchange (next try-block) still saved.
        bg_mocks.memory.store_exchange.assert_awaited_once()

    async def test_exchange_storage_failure_isolated(self, bg_mocks):
        """A crash storing the exchange must not stop episode creation downstream."""
        bg_mocks.memory.store_exchange.side_effect = RuntimeError("pg down")
        with patch.object(bg, "create_episode_summary",
                          new=AsyncMock(return_value="An episode.")):
            await bg.background_extraction("m", "r", _session(turn_count=10), user_id="u1")
        # Exchange failed, but the 10-turn episode still got stored.
        bg_mocks.memory.store_episode.assert_awaited_once()


# ── background_compaction ─────────────────────────────────────────────────────


class TestCompaction:
    async def test_below_threshold_is_noop(self, bg_mocks):
        session = _session(turn_count=4)
        with patch("app.fact_extractor.compact_turns", new=AsyncMock()) as ct:
            await bg.background_compaction(session, user_id="u1")
        ct.assert_not_awaited()
        bg_mocks.memory.save_session.assert_not_awaited()

    async def test_at_threshold_compacts_and_keeps_recent_raw(self, bg_mocks):
        session = _session(turn_count=10)
        with patch("app.fact_extractor.compact_turns",
                   new=AsyncMock(return_value="Condensed summary.")):
            await bg.background_compaction(session, user_id="u1")
        # Keeps exactly COMPACT_KEEP_RAW recent turns + the new summary.
        assert session.context_summary == "Condensed summary."
        assert len(session.turns) == bg_mocks.context.COMPACT_KEEP_RAW
        bg_mocks.memory.save_session.assert_awaited_once()

    async def test_existing_summary_prepended_to_compaction_input(self, bg_mocks):
        session = _session(turn_count=10, context_summary="Older context.")
        ct = AsyncMock(return_value="New rollup.")
        with patch("app.fact_extractor.compact_turns", new=ct):
            await bg.background_compaction(session, user_id="u1")
        passed_turns = ct.await_args.args[0]
        assert passed_turns[0]["role"] == "system"
        assert "Older context." in passed_turns[0]["content"]

    async def test_empty_summary_keeps_raw_turns_unchanged(self, bg_mocks):
        session = _session(turn_count=10)
        original = list(session.turns)
        with patch("app.fact_extractor.compact_turns", new=AsyncMock(return_value="")):
            await bg.background_compaction(session, user_id="u1")
        assert session.turns == original
        assert session.context_summary is None
        bg_mocks.memory.save_session.assert_not_awaited()

    async def test_compaction_exception_swallowed(self, bg_mocks):
        session = _session(turn_count=10)
        original_turns = list(session.turns)
        ct = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("app.fact_extractor.compact_turns", new=ct):
            await bg.background_compaction(session, user_id="u1")  # must not raise
        # The failure was reached but isolated: no partial mutation, no save.
        assert ct.await_count == 1
        assert session.turns == original_turns
        assert session.context_summary is None
        bg_mocks.memory.save_session.assert_not_awaited()


# ── background_image_gen ──────────────────────────────────────────────────────


class TestImageGen:
    async def test_sleeps_then_checks_comfyui_and_sends_thinking(self, bg_mocks):
        with patch("app.image_gen.check_comfyui_ready", new=AsyncMock(return_value=True)), \
             patch.object(bg, "is_couple_scene", return_value=False), \
             patch.object(bg, "is_landscape", return_value=False), \
             patch.object(bg, "_enhance_image_prompt", return_value="tags"), \
             patch.object(bg, "build_prompt", return_value="full prompt"), \
             patch.object(bg, "generate_image", new=AsyncMock(return_value=None)):
            await bg.background_image_gen("draw a sunset", user_id="u1")
        assert bg_mocks.sleep.await_args_list[0].args == (1,)
        bg_mocks.ws.send_thinking.assert_awaited()

    async def test_portrait_dimensions_for_non_landscape(self, bg_mocks):
        gen = AsyncMock(return_value=None)
        with patch("app.image_gen.check_comfyui_ready", new=AsyncMock(return_value=True)), \
             patch.object(bg, "is_couple_scene", return_value=False), \
             patch.object(bg, "is_landscape", return_value=False), \
             patch.object(bg, "_enhance_image_prompt", return_value="tags"), \
             patch.object(bg, "build_prompt", return_value="p"), \
             patch.object(bg, "generate_image", new=gen):
            await bg.background_image_gen("portrait of klukai", user_id="u1")
        assert gen.await_args.kwargs == {"width": 832, "height": 1216}

    async def test_landscape_dimensions_swapped(self, bg_mocks):
        gen = AsyncMock(return_value=None)
        with patch("app.image_gen.check_comfyui_ready", new=AsyncMock(return_value=True)), \
             patch.object(bg, "is_couple_scene", return_value=False), \
             patch.object(bg, "is_landscape", return_value=True), \
             patch.object(bg, "_enhance_image_prompt", return_value="tags"), \
             patch.object(bg, "build_prompt", return_value="p"), \
             patch.object(bg, "generate_image", new=gen):
            await bg.background_image_gen("wide vista", user_id="u1")
        assert gen.await_args.kwargs == {"width": 1216, "height": 832}

    async def test_successful_image_archived_and_sent(self, bg_mocks):
        with patch("app.image_gen.check_comfyui_ready", new=AsyncMock(return_value=True)), \
             patch.object(bg, "is_couple_scene", return_value=False), \
             patch.object(bg, "is_landscape", return_value=False), \
             patch.object(bg, "_enhance_image_prompt", return_value="tags"), \
             patch.object(bg, "build_prompt", return_value="full prompt"), \
             patch.object(bg, "generate_image", new=AsyncMock(return_value=b"PNGDATA")):
            await bg.background_image_gen("draw klukai", user_id="u1")
        bg_mocks.memory_archive.save_image.assert_awaited_once()
        bg_mocks.context.set_last_memory_id.assert_called_once_with("u1", "mem-123")
        # The image frame carries the archived memory_id.
        img_frames = [c for c in bg_mocks.ws.send.await_args_list
                      if c.args[1].get("type") == "image"]
        assert img_frames and img_frames[0].args[1]["memory_id"] == "mem-123"
        bg_mocks.proactive.record_first.assert_any_await("u1", "first_image")

    async def test_failed_generation_sends_graceful_message(self, bg_mocks):
        with patch("app.image_gen.check_comfyui_ready", new=AsyncMock(return_value=True)), \
             patch.object(bg, "is_couple_scene", return_value=False), \
             patch.object(bg, "is_landscape", return_value=False), \
             patch.object(bg, "_enhance_image_prompt", return_value="tags"), \
             patch.object(bg, "build_prompt", return_value="p"), \
             patch.object(bg, "generate_image", new=AsyncMock(return_value=None)):
            await bg.background_image_gen("draw", user_id="u1")
        # User is never ghosted: a proactive failure message + a thinking-clear.
        bg_mocks.ws.send_proactive.assert_awaited()
        clear_frames = [c for c in bg_mocks.ws.send.await_args_list
                        if c.args[1] == {"type": "thinking", "content": ""}]
        assert clear_frames

    async def test_comfyui_warmup_message_when_not_ready(self, bg_mocks):
        with patch("app.image_gen.check_comfyui_ready", new=AsyncMock(return_value=False)), \
             patch.object(bg, "is_couple_scene", return_value=False), \
             patch.object(bg, "is_landscape", return_value=False), \
             patch.object(bg, "_enhance_image_prompt", return_value="tags"), \
             patch.object(bg, "build_prompt", return_value="p"), \
             patch.object(bg, "generate_image", new=AsyncMock(return_value=None)):
            await bg.background_image_gen("draw", user_id="u1")
        warmups = [c for c in bg_mocks.ws.send_thinking.await_args_list
                   if "Warming up" in c.args[1]]
        assert warmups

    async def test_exception_path_clears_thinking_bubble(self, bg_mocks):
        """If generate_image raises, the except block still surfaces a message."""
        with patch("app.image_gen.check_comfyui_ready", new=AsyncMock(return_value=True)), \
             patch.object(bg, "is_couple_scene", return_value=False), \
             patch.object(bg, "is_landscape", return_value=False), \
             patch.object(bg, "_enhance_image_prompt", return_value="tags"), \
             patch.object(bg, "build_prompt", return_value="p"), \
             patch.object(bg, "generate_image",
                          new=AsyncMock(side_effect=RuntimeError("GPU OOM"))):
            await bg.background_image_gen("draw", user_id="u1")  # must not raise
        clear_frames = [c for c in bg_mocks.ws.send.await_args_list
                        if c.args[1] == {"type": "thinking", "content": ""}]
        assert clear_frames

    async def test_affection_level_threaded_into_prompt(self, bg_mocks):
        bg_mocks.affection.get_state.return_value = _aff_state(8)
        build = MagicMock(return_value="prompt")
        with patch("app.image_gen.check_comfyui_ready", new=AsyncMock(return_value=True)), \
             patch.object(bg, "is_couple_scene", return_value=True), \
             patch.object(bg, "is_landscape", return_value=False), \
             patch.object(bg, "_enhance_image_prompt", return_value="tags"), \
             patch.object(bg, "build_prompt", new=build), \
             patch.object(bg, "generate_image", new=AsyncMock(return_value=None)):
            await bg.background_image_gen("us together", user_id="u1")
        assert build.call_args.kwargs["affection_level"] == 8
        assert build.call_args.kwargs["couple"] is True

    async def test_unlocked_costume_threaded_into_prompt(self, bg_mocks):
        bg_mocks.affection.get_state.return_value = _aff_state(8)
        bg_mocks.memory.recall_fact = AsyncMock(return_value="astral_luminous")  # unlock 4
        build = MagicMock(return_value="prompt")
        with patch("app.image_gen.check_comfyui_ready", new=AsyncMock(return_value=True)), \
             patch.object(bg, "is_couple_scene", return_value=False), \
             patch.object(bg, "is_landscape", return_value=False), \
             patch.object(bg, "_enhance_image_prompt", return_value="tags"), \
             patch.object(bg, "build_prompt", new=build), \
             patch.object(bg, "generate_image", new=AsyncMock(return_value=None)):
            await bg.background_image_gen("draw yourself", user_id="u1")
        assert build.call_args.kwargs["costume"] == "astral_luminous"

    async def test_locked_costume_not_threaded(self, bg_mocks):
        bg_mocks.affection.get_state.return_value = _aff_state(2)  # below astral_luminous (4)
        bg_mocks.memory.recall_fact = AsyncMock(return_value="astral_luminous")
        build = MagicMock(return_value="prompt")
        with patch("app.image_gen.check_comfyui_ready", new=AsyncMock(return_value=True)), \
             patch.object(bg, "is_couple_scene", return_value=False), \
             patch.object(bg, "is_landscape", return_value=False), \
             patch.object(bg, "_enhance_image_prompt", return_value="tags"), \
             patch.object(bg, "build_prompt", new=build), \
             patch.object(bg, "generate_image", new=AsyncMock(return_value=None)):
            await bg.background_image_gen("draw yourself", user_id="u1")
        assert build.call_args.kwargs["costume"] is None


# ── background_recall ─────────────────────────────────────────────────────────


class TestRecall:
    async def test_no_match_sends_apology(self, bg_mocks):
        bg_mocks.memory_archive.recall_memory.return_value = None
        await bg.background_recall("remember the beach?", _session(1), "u1")
        bg_mocks.ws.send_proactive.assert_awaited_once()
        assert "nothing matched" in bg_mocks.ws.send_proactive.await_args.args[1]

    async def test_match_sends_card_and_image(self, bg_mocks):
        bg_mocks.memory_archive.recall_memory.return_value = {
            "id": "m9", "category": "candid",
            "annotation": "A quiet evening.", "created_at": None,
        }
        bg_mocks.memory_archive.get_image_bytes.return_value = b"IMG"
        await bg.background_recall("our evening", _session(1), "u1")
        # Card text references the category + annotation.
        card_call = bg_mocks.ws.send_proactive.await_args.args[1]
        assert "[candid]" in card_call and "A quiet evening." in card_call
        # Image frame sent with the recalled memory id.
        img_frames = [c for c in bg_mocks.ws.send.await_args_list
                      if c.args[1].get("type") == "image"]
        assert img_frames and img_frames[0].args[1]["memory_id"] == "m9"

    async def test_recall_time_ref_yesterday(self, bg_mocks):
        from datetime import datetime, timezone, timedelta
        yesterday = datetime.now(timezone.utc) - timedelta(days=1, hours=2)
        bg_mocks.memory_archive.recall_memory.return_value = {
            "id": "m1", "category": "milestone",
            "annotation": "First mission.", "created_at": yesterday.isoformat(),
        }
        bg_mocks.memory_archive.get_image_bytes.return_value = None
        await bg.background_recall("first op", _session(1), "u1")
        assert "yesterday" in bg_mocks.ws.send_proactive.await_args.args[1]

    async def test_recall_time_ref_earlier_today(self, bg_mocks):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc)
        bg_mocks.memory_archive.recall_memory.return_value = {
            "id": "m3", "category": "candid",
            "annotation": "A laugh.", "created_at": today,
        }
        bg_mocks.memory_archive.get_image_bytes.return_value = None
        await bg.background_recall("x", _session(1), "u1")
        assert "earlier today" in bg_mocks.ws.send_proactive.await_args.args[1]

    async def test_recall_time_ref_n_days_ago(self, bg_mocks):
        from datetime import datetime, timezone, timedelta
        three_days = datetime.now(timezone.utc) - timedelta(days=3, hours=1)
        bg_mocks.memory_archive.recall_memory.return_value = {
            "id": "m4", "category": "candid",
            "annotation": "An outing.", "created_at": three_days,
        }
        bg_mocks.memory_archive.get_image_bytes.return_value = None
        await bg.background_recall("x", _session(1), "u1")
        assert "3 days ago" in bg_mocks.ws.send_proactive.await_args.args[1]

    async def test_recall_time_ref_old_date_formatted(self, bg_mocks):
        from datetime import datetime, timezone, timedelta
        old = datetime.now(timezone.utc) - timedelta(days=40)
        bg_mocks.memory_archive.recall_memory.return_value = {
            "id": "m5", "category": "milestone",
            "annotation": "Long ago.", "created_at": old,
        }
        bg_mocks.memory_archive.get_image_bytes.return_value = None
        await bg.background_recall("x", _session(1), "u1")
        # Falls through to the "%B %d" strftime branch (e.g. "April 15").
        card = bg_mocks.ws.send_proactive.await_args.args[1]
        assert old.strftime("%B %d") in card

    async def test_recall_naive_datetime_gets_utc_tzinfo(self, bg_mocks):
        """A naive created_at string is coerced to UTC before the delta math."""
        from datetime import datetime, timedelta
        naive = (datetime.utcnow() - timedelta(days=1, hours=3)).isoformat()
        bg_mocks.memory_archive.recall_memory.return_value = {
            "id": "m6", "category": "candid",
            "annotation": "A memory.", "created_at": naive,
        }
        bg_mocks.memory_archive.get_image_bytes.return_value = None
        await bg.background_recall("x", _session(1), "u1")  # must not raise on tz math
        bg_mocks.ws.send_proactive.assert_awaited()

    async def test_recall_default_annotation_when_blank(self, bg_mocks):
        bg_mocks.memory_archive.recall_memory.return_value = {
            "id": "m2", "category": "", "annotation": "", "created_at": None,
        }
        bg_mocks.memory_archive.get_image_bytes.return_value = None
        await bg.background_recall("x", _session(1), "u1")
        assert "A moment I've preserved." in bg_mocks.ws.send_proactive.await_args.args[1]

    async def test_recall_exception_swallowed(self, bg_mocks):
        bg_mocks.memory_archive.recall_memory.side_effect = RuntimeError("qdrant down")
        await bg.background_recall("x", _session(1), "u1")  # must not raise
        # Failure happened before any card/image could be sent.
        bg_mocks.memory_archive.recall_memory.assert_awaited_once()
        bg_mocks.ws.send_proactive.assert_not_awaited()
        bg_mocks.ws.send.assert_not_awaited()


# ── do_memory_keep ────────────────────────────────────────────────────────────


class TestMemoryKeep:
    async def test_keep_marks_commander(self, bg_mocks):
        await bg.do_memory_keep("mem-7", kept=True)
        bg_mocks.memory_archive.update_kept.assert_awaited_once_with(
            "mem-7", kept=True, kept_by="commander"
        )

    async def test_discard_marks_discarded(self, bg_mocks):
        await bg.do_memory_keep("mem-7", kept=False)
        assert bg_mocks.memory_archive.update_kept.await_args.kwargs["kept_by"] == "discarded"

    async def test_keep_exception_swallowed(self, bg_mocks):
        bg_mocks.memory_archive.update_kept.side_effect = RuntimeError("db error")
        await bg.do_memory_keep("mem-7", kept=True)  # must not raise
        # The update was attempted (and failed) — proving the except path ran.
        bg_mocks.memory_archive.update_kept.assert_awaited_once_with(
            "mem-7", kept=True, kept_by="commander"
        )



# ── background_extraction: malformed LLM fact payloads ───────────────────────


class TestBackgroundExtractionMalformedFacts:
    """A bad facts list must not abort mood/affection/exchange persistence.

    Pre-fix, `fact["key"]` on a string raised TypeError which the outer
    except swallowed — silently dropping store_exchange for that turn.
    """

    @pytest.mark.asyncio
    async def test_string_facts_skipped_and_exchange_still_stored(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            facts=["he likes coffee", "also motorcycles"],
            should_remember=True,
            mood="composed",
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.memory.set_relationship_fact.assert_not_awaited()
        bg_mocks.memory.store_exchange.assert_awaited_once()
        # importance should still reflect should_remember
        kwargs = bg_mocks.memory.store_exchange.call_args.kwargs
        assert kwargs["importance"] == 0.7

    @pytest.mark.asyncio
    async def test_mixed_facts_stores_only_valid_dicts(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            facts=[
                "bare string",
                {"key": "favorite_drink", "value": "black coffee"},
                {"key": 123, "value": "bad key type"},
                {"key": "ok", "value": None},
                {"key": "squad", "value": "404"},
            ],
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        # Only the two well-formed string key/value pairs
        calls = bg_mocks.memory.set_relationship_fact.await_args_list
        stored = {(c.args[0], c.args[1]) for c in calls}
        assert stored == {("favorite_drink", "black coffee"), ("squad", "404")}
        bg_mocks.memory.store_exchange.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_list_topics_still_stores_exchange(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            topics="single-topic-string",
            should_remember=False,
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        kwargs = bg_mocks.memory.store_exchange.call_args.kwargs
        assert kwargs["topics"] == ["single-topic-string"]
        assert kwargs["importance"] == 0.4
