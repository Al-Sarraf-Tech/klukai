"""Behavioral tests for the Inside Jokes & Growth Arc feature.

Covers three feature surfaces, all with mocked DB/LLM (no live services):

PRIMARY  — the one-time level-9 "Oath Fulfilled" capstone in background.py:
           lv8→9 fires the distinctive oath scene EXACTLY once (guarded by the
           companion_firsts table via proactive.record_first); a repeat arrival
           at lv9 (record_first→False) gets only the short level_up line; a
           non-9 level-up never touches the oath path.
SECONDARY — inside jokes: fact_extractor validation, the memory store/recall
           roundtrip (rel:joke:* namespace, dossier exclusion), background
           wiring, and the per-message surfacing block.
TERTIARY  — growth arc: the cadence/affection-gated per-message block.

Mirrors the patterns in test_background_coverage.py (the bg_mocks fixture,
SimpleNamespace stand-ins) and test_memory_facts.py (AsyncMock'd _http).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("psycopg")

import app.background as bg
from app.memory import MemoryManager
from app.models import SessionState
from app.personality.memory_blocks import build_inside_jokes_block
from app.personality.state_blocks import build_growth_arc_block


# ── Shared helpers (mirror test_background_coverage.py) ───────────────────────


def _session(turn_count: int = 1, *, mood: str = "composed") -> SessionState:
    turns = [{"role": "user", "content": f"Turn {i}"} for i in range(turn_count)]
    return SessionState(
        conversation_id="conv-1", turns=turns, context_summary=None,
        mood=mood, turn_count=turn_count,
    )


def _aff_state(level: int = 8):
    return SimpleNamespace(level=level, score=level * 100)


def _aff_change(*, new_score=820, new_level=9, new_level_name="Devoted Oath",
                delta=10, level_changed=True, level_direction="up"):
    return SimpleNamespace(
        new_score=new_score, new_level=new_level, new_level_name=new_level_name,
        delta=delta, level_changed=level_changed, level_direction=level_direction,
    )


def _base_extract_result(**overrides) -> dict:
    result = {
        "facts": [], "mood": "composed", "topics": [], "should_remember": False,
        "interaction": {"type": "neutral", "intensity": 5},
        "commander_details": {}, "gift_item": None, "inside_joke": None,
    }
    result.update(overrides)
    return result


_OATH_SCENE = [
    "...Commander. Stay.",
    "Indigo gown. I chose you. Every day, I choose you again. The oath is fulfilled.",
    "...Thank you. For answering.",
]


@pytest.fixture
def bg_mocks(monkeypatch):
    """Patch every collaborator background.background_extraction reaches.

    Defaults are tuned for the oath path: affection rises to level 9, the
    transition is a level-up, and the personality config carries the oath
    scene plus a level-9 short line. record_first defaults to True (first
    ever) so the oath fires unless a test overrides it.
    """
    monkeypatch.setattr(bg.asyncio, "sleep", AsyncMock())

    affection = MagicMock()
    affection.get_state = AsyncMock(return_value=_aff_state(8))
    affection.apply_classification = AsyncMock(return_value=_aff_change())

    memory = MagicMock()
    memory.set_relationship_fact = AsyncMock()
    memory.set_inside_joke = AsyncMock()
    memory.save_session = AsyncMock()
    memory.store_exchange = AsyncMock()
    memory.store_episode = AsyncMock()
    memory.record_milestone = AsyncMock(return_value=True)
    memory.get_session = AsyncMock(return_value=_session(2))

    ws = MagicMock()
    for name in ("send", "send_mood", "send_proactive", "send_thinking",
                 "send_affection", "send_affection_level_change",
                 "send_heartbeat_spike"):
        setattr(ws, name, AsyncMock())

    proactive = MagicMock()
    proactive.store_gift = AsyncMock()
    proactive.record_first = AsyncMock(return_value=True)
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
    memory_archive.save_image = AsyncMock(return_value="mem-1")

    extract_facts = AsyncMock(return_value=_base_extract_result())

    monkeypatch.setattr(bg, "affection", affection)
    monkeypatch.setattr(bg, "memory", memory)
    monkeypatch.setattr(bg, "ws", ws)
    monkeypatch.setattr(bg, "proactive", proactive)
    monkeypatch.setattr(bg, "context", context_mod)
    monkeypatch.setattr(bg, "memory_archive", memory_archive)
    monkeypatch.setattr(bg, "extract_facts", extract_facts)
    monkeypatch.setattr(bg, "session_id", lambda uid: f"session:{uid}")
    monkeypatch.setattr(bg, "load_personality", MagicMock(return_value={
        "affection": {
            "oath_fulfilled_scene": list(_OATH_SCENE),
            "level_up_messages": {9: "Every day, I choose you again."},
            "milestone_scenes": {9: ["generic lv9 line"]},
        }
    }))

    return SimpleNamespace(
        affection=affection, memory=memory, ws=ws, proactive=proactive,
        context=context_mod, memory_archive=memory_archive,
        extract_facts=extract_facts,
    )


# ══ PRIMARY: Oath Fulfilled level-9 capstone ═════════════════════════════════


class TestOathFulfilledCapstone:
    async def test_oath_scene_fires_on_first_level_9(self, bg_mocks):
        """First arrival at lv9 delivers every oath-scene line as a proactive."""
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        sent = [c.args[1] for c in bg_mocks.ws.send_proactive.await_args_list]
        for line in _OATH_SCENE:
            assert line in sent, f"missing oath line: {line!r}"

    async def test_oath_guarded_by_record_first_oath_fulfilled(self, bg_mocks):
        """The once-ever guard is companion_firsts via record_first('oath_fulfilled')."""
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.proactive.record_first.assert_any_await("u1", "oath_fulfilled")

    async def test_oath_does_not_repeat_when_already_recorded(self, bg_mocks):
        """A second arrival at lv9 (record_first→False) suppresses the oath scene
        and the generic milestone scene; only the short level-up line is sent."""
        bg_mocks.proactive.record_first.return_value = False  # already happened
        bg_mocks.memory.record_milestone.return_value = False  # milestone seen too
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        sent = [c.args[1] for c in bg_mocks.ws.send_proactive.await_args_list]
        # No oath lines.
        assert not any(line in sent for line in _OATH_SCENE)
        # No generic milestone scene line either.
        assert "generic lv9 line" not in sent
        # Just the short repeat-level-up line.
        assert "Every day, I choose you again." in sent

    async def test_oath_suppresses_generic_milestone_scene_on_first_time(self, bg_mocks):
        """When the oath fires, the ordinary milestone_scenes[9] line is NOT also
        sent — the oath IS the level-9 moment."""
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        sent = [c.args[1] for c in bg_mocks.ws.send_proactive.await_args_list]
        assert "generic lv9 line" not in sent

    async def test_oath_records_milestone_so_it_is_not_redelivered(self, bg_mocks):
        """The oath path still records the user-scoped affection_level_9 milestone
        so a later lv9 return won't trigger the generic scene."""
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.memory.record_milestone.assert_any_await(
            "affection_level_9", user_id="u1"
        )

    async def test_no_oath_below_level_9(self, bg_mocks):
        """A level-up that lands below 9 never touches the oath path."""
        bg_mocks.affection.apply_classification.return_value = _aff_change(
            new_level=8, new_level_name="Bonded"
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        # record_first was never called with the oath event type.
        calls = [c.args for c in bg_mocks.proactive.record_first.await_args_list]
        assert ("u1", "oath_fulfilled") not in calls
        sent = [c.args[1] for c in bg_mocks.ws.send_proactive.await_args_list]
        assert not any(line in sent for line in _OATH_SCENE)

    async def test_no_oath_when_level_unchanged(self, bg_mocks):
        """No level change at all → no oath, regardless of being at level 9."""
        bg_mocks.affection.apply_classification.return_value = _aff_change(
            level_changed=False, level_direction=""
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        calls = [c.args for c in bg_mocks.proactive.record_first.await_args_list]
        assert ("u1", "oath_fulfilled") not in calls


# ══ SECONDARY: Inside jokes ══════════════════════════════════════════════════


class TestInsideJokeExtraction:
    """fact_extractor.extract_facts validates the inside_joke field."""

    async def _run(self, raw_joke):
        from app import fact_extractor as fe
        result = {
            "mood": "playful",
            "interaction": {"type": "neutral", "intensity": 5},
            "inside_joke": raw_joke,
        }
        with patch.object(fe, "call_llm", new=AsyncMock(return_value=result)), \
                patch("app.llm_router.get_lm_gate", return_value=AsyncMock()):
            return await fe.extract_facts("u", "a", affection_level=5)

    async def test_valid_inside_joke_passes_through(self):
        out = await self._run({"label": "Klukadile denial", "note": "she denies the plush"})
        assert out["inside_joke"] == {"label": "Klukadile denial", "note": "she denies the plush"}

    async def test_bare_string_inside_joke_dropped(self):
        out = await self._run("just a string")
        assert out["inside_joke"] is None

    async def test_missing_note_dropped(self):
        out = await self._run({"label": "only a label"})
        assert out["inside_joke"] is None

    async def test_blank_fields_dropped(self):
        out = await self._run({"label": "  ", "note": "  "})
        assert out["inside_joke"] is None

    async def test_default_when_absent(self):
        from app import fact_extractor as fe
        result = {"mood": "composed", "interaction": {"type": "neutral", "intensity": 5}}
        with patch.object(fe, "call_llm", new=AsyncMock(return_value=result)), \
                patch("app.llm_router.get_lm_gate", return_value=AsyncMock()):
            out = await fe.extract_facts("u", "a")
        assert out["inside_joke"] is None


class TestInsideJokeStore:
    """MemoryManager inside-joke store/recall on the rel:joke:* namespace."""

    def _mk(self):
        m = MemoryManager()
        m._http = AsyncMock()
        return m

    def test_slug_is_stable_and_safe(self):
        assert MemoryManager._joke_slug("The Klukadile Denial!!") == "the_klukadile_denial"
        assert MemoryManager._joke_slug("   ") == "ref"
        assert len(MemoryManager._joke_slug("x" * 200)) <= 60

    async def test_set_inside_joke_stores_under_joke_namespace(self):
        m = self._mk()
        with patch.object(m, "store_fact", new=AsyncMock()) as sf:
            await m.set_inside_joke("Crocodile Tears", "her weapon pun", "alice")
        key, value = sf.await_args.args[0], sf.await_args.args[1]
        assert key == "rel:joke:crocodile_tears"
        assert value == "Crocodile Tears :: her weapon pun"
        assert sf.await_args.kwargs["user_id"] == "alice"

    async def test_set_inside_joke_skips_blank(self):
        m = self._mk()
        with patch.object(m, "store_fact", new=AsyncMock()) as sf:
            await m.set_inside_joke("", "note", "alice")
            await m.set_inside_joke("label", "  ", "alice")
        sf.assert_not_awaited()

    async def test_get_inside_jokes_parses_label_note(self):
        m = self._mk()
        entries = [
            {"key": "companion:alice:rel:joke:croc", "value": "Crocodile Tears :: her weapon pun"},
            {"key": "companion:alice:rel:joke:plush", "value": "Klukadile :: the denied plush"},
        ]
        with patch.object(m, "recall_facts_by_pattern",
                          new=AsyncMock(return_value=entries)) as rfp:
            jokes = await m.get_inside_jokes("alice")
            # Queries the joke sub-namespace, not all rel:*.
            rfp.assert_awaited_once_with("rel:joke:%", user_id="alice")
        assert {"label": "Crocodile Tears", "note": "her weapon pun"} in jokes
        assert {"label": "Klukadile", "note": "the denied plush"} in jokes

    async def test_get_inside_jokes_degrades_on_malformed_value(self):
        m = self._mk()
        entries = [{"key": "companion:alice:rel:joke:x", "value": "no separator here"}]
        with patch.object(m, "recall_facts_by_pattern", new=AsyncMock(return_value=entries)):
            jokes = await m.get_inside_jokes("alice")
        assert jokes == [{"label": "no separator here", "note": ""}]

    async def test_dossier_excludes_inside_jokes(self):
        """get_relationship_facts must NOT surface rel:joke:* entries (they have
        their own block); only true dossier facts come through."""
        m = self._mk()
        entries = [
            {"key": "companion:alice:rel:birthday", "value": "March 5"},
            {"key": "companion:alice:rel:joke:croc", "value": "Crocodile Tears :: pun"},
        ]
        with patch.object(m, "recall_facts_by_pattern", new=AsyncMock(return_value=entries)):
            facts = await m.get_relationship_facts("alice")
        assert facts == {"birthday": "March 5"}
        assert not any(k.startswith("joke:") for k in facts)


class TestInsideJokeBackgroundWiring:
    """background_extraction persists a detected inside joke via memory.set_inside_joke."""

    async def test_inside_joke_stored_when_detected(self, bg_mocks):
        bg_mocks.extract_facts.return_value = _base_extract_result(
            inside_joke={"label": "Klukadile denial", "note": "she denies the plush"}
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.memory.set_inside_joke.assert_awaited_once_with(
            "Klukadile denial", "she denies the plush", user_id="u1"
        )

    async def test_no_inside_joke_stored_when_absent(self, bg_mocks):
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.memory.set_inside_joke.assert_not_awaited()

    async def test_partial_inside_joke_not_stored(self, bg_mocks):
        """A dict missing string fields is skipped at the background layer too."""
        bg_mocks.extract_facts.return_value = _base_extract_result(
            inside_joke={"label": 123, "note": None}
        )
        await bg.background_extraction("m", "r", _session(1), user_id="u1")
        bg_mocks.memory.set_inside_joke.assert_not_awaited()


class TestInsideJokeBlock:
    """The per-message surfacing block (build_inside_jokes_block)."""

    _JOKES = [
        {"label": "Klukadile denial", "note": "she denies the plush"},
        {"label": "Crocodile Tears", "note": "her weapon pun"},
        {"label": "Third bit", "note": "extra"},
    ]

    def test_surfaces_capped_count(self):
        block = build_inside_jokes_block(self._JOKES, 6, max_surfaced=2, min_affection_level=3)
        assert "Klukadile denial" in block
        assert "Crocodile Tears" in block
        assert "Third bit" not in block  # capped at 2

    def test_empty_below_min_affection(self):
        assert build_inside_jokes_block(self._JOKES, 2, min_affection_level=3) == ""

    def test_empty_when_no_jokes(self):
        assert build_inside_jokes_block([], 9) == ""
        assert build_inside_jokes_block(None, 9) == ""

    def test_label_only_entries_render(self):
        block = build_inside_jokes_block([{"label": "bit", "note": ""}], 5, min_affection_level=3)
        assert "bit" in block

    def test_all_labelless_suppresses_block(self):
        block = build_inside_jokes_block([{"label": "", "note": "x"}], 9, min_affection_level=3)
        assert block == ""


# ══ TERTIARY: Growth arc ═════════════════════════════════════════════════════


class TestGrowthArcBlock:
    _CFG = {
        "growth_arc": {
            "enabled": True, "min_affection_level": 4, "turn_interval": 7,
            "goals": ["Goal A", "Goal B", "Goal C"],
        }
    }

    def test_surfaces_on_cadence(self):
        block = build_growth_arc_block(self._CFG, 5, 7)
        assert "QUIET ASPIRATION" in block
        assert "Goal A" in block

    def test_empty_off_cadence(self):
        assert build_growth_arc_block(self._CFG, 5, 8) == ""

    def test_empty_below_min_affection(self):
        assert build_growth_arc_block(self._CFG, 3, 7) == ""

    def test_empty_when_disabled(self):
        cfg = {"growth_arc": {"enabled": False, "goals": ["x"], "turn_interval": 7,
                              "min_affection_level": 4}}
        assert build_growth_arc_block(cfg, 9, 7) == ""

    def test_empty_when_no_goals(self):
        cfg = {"growth_arc": {"enabled": True, "goals": [], "turn_interval": 7,
                              "min_affection_level": 4}}
        assert build_growth_arc_block(cfg, 9, 7) == ""

    def test_empty_when_section_absent(self):
        assert build_growth_arc_block({}, 9, 7) == ""

    def test_goal_rotates_across_intervals(self):
        # turn 7 → slot 0 (Goal A), turn 14 → slot 1 (Goal B), turn 21 → slot 2 (Goal C)
        assert "Goal A" in build_growth_arc_block(self._CFG, 5, 7)
        assert "Goal B" in build_growth_arc_block(self._CFG, 5, 14)
        assert "Goal C" in build_growth_arc_block(self._CFG, 5, 21)
        # wraps back to Goal A at turn 28
        assert "Goal A" in build_growth_arc_block(self._CFG, 5, 28)

    def test_deterministic_for_same_turn(self):
        assert build_growth_arc_block(self._CFG, 5, 7) == build_growth_arc_block(self._CFG, 5, 7)
