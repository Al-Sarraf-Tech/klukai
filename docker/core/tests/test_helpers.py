"""Tests for main.py helper functions: narration, image prompts, text processing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.helpers import (
    chunk_text as _chunk_text,
    enhance_image_prompt as _enhance_image_prompt,
    fix_narration as _fix_narration,
    parse_interval_minutes as _parse_interval_minutes,
    strip_actions_for_tts as _strip_actions_for_tts,
    wants_mission_cancel as _wants_mission_cancel,
    wants_mission_start as _wants_mission_start,
    wants_recall as _wants_recall,
)


# ── Narration fix ────────────────────────────────────────────────────────────


class TestFixNarration:
    """Klukai speaks in first person. Second-person narration must be corrected."""

    def test_you_to_i_conversion(self):
        assert _fix_narration("(You smile softly)") == "(I smile softly)"

    def test_your_to_my_conversion(self):
        assert _fix_narration("(Your hand trembles)") == "(My hand trembles)"

    def test_lowercase_your(self):
        assert _fix_narration("(your eyes widen)") == "(my eyes widen)"

    def test_strips_commander_narration(self):
        result = _fix_narration("Hello. (A blush crosses your face.) How are you?")
        assert "crosses your" not in result
        assert "Hello." in result

    def test_strips_think_tags(self):
        result = _fix_narration("<think>internal reasoning</think>Visible text")
        assert "internal reasoning" not in result
        assert "Visible text" in result

    def test_strips_think_pipe_tags(self):
        result = _fix_narration("<|think|>reasoning<|/think|>Response")
        assert "reasoning" not in result
        assert "Response" in result

    def test_strips_trailing_pipes(self):
        assert _fix_narration("Hello world|||") == "Hello world"

    def test_collapses_double_spaces(self):
        result = _fix_narration("Hello  (your eyes widen)  world")
        assert "  " not in result

    def test_preserves_normal_parenthetical(self):
        result = _fix_narration("(I lean forward) Tell me more.")
        assert "(I lean forward)" in result

    def test_empty_string(self):
        assert _fix_narration("") == ""

    def test_multiline_think_tags(self):
        text = "<think>\nLong\nmultiline\nreasoning\n</think>Final answer."
        assert _fix_narration(text) == "Final answer."


# ── Image prompt enhancement ─────────────────────────────────────────────────


class TestEnhanceImagePrompt:
    """Keyword-based scene detection for Illustrious image generation."""

    def test_sunset_scene(self):
        result = _enhance_image_prompt("draw us watching the sunset")
        assert "sunset" in result
        assert "golden hour" in result

    def test_motorcycle_scene(self):
        result = _enhance_image_prompt("riding motorcycle together")
        assert "motorcycle" in result
        assert "riding" in result

    def test_bedroom_scene(self):
        result = _enhance_image_prompt("lying in bed together")
        assert "bedroom" in result
        assert "intimate" in result

    def test_kiss_action(self):
        result = _enhance_image_prompt("kiss me")
        assert "kiss" in result
        assert "romantic" in result

    def test_no_match_gives_generic(self):
        result = _enhance_image_prompt("hello there")
        assert "standing" in result
        assert "looking at viewer" in result

    def test_multiple_tags_combine(self):
        result = _enhance_image_prompt("sunset beach kiss")
        assert "sunset" in result
        assert "beach" in result
        assert "kiss" in result

    def test_empty_string(self):
        result = _enhance_image_prompt("")
        assert "standing" in result  # generic fallback


# ── Text chunking ────────────────────────────────────────────────────────────


class TestChunkText:
    def test_basic_chunking(self):
        chunks = _chunk_text("Hello, World!", 5)
        assert chunks == ["Hello", ", Wor", "ld!"]

    def test_exact_boundary(self):
        chunks = _chunk_text("abcd", 2)
        assert chunks == ["ab", "cd"]

    def test_empty_string(self):
        assert _chunk_text("") == []

    def test_default_size(self):
        chunks = _chunk_text("12345678abcdefgh")
        assert len(chunks) == 2
        assert chunks[0] == "12345678"


# ── TTS action stripping ────────────────────────────────────────────────────


class TestStripActionsForTTS:
    def test_removes_parenthetical(self):
        assert _strip_actions_for_tts("Hello. (I smile.) World.") == "Hello. World."

    def test_handles_multiple_actions(self):
        result = _strip_actions_for_tts("(I lean closer) Yes. (I nod) Indeed.")
        assert result == "Yes. Indeed."

    def test_preserves_plain_text(self):
        assert _strip_actions_for_tts("No actions here.") == "No actions here."

    def test_collapses_whitespace(self):
        result = _strip_actions_for_tts("Start  (action)  end")
        assert "  " not in result


# ── Intent detection ─────────────────────────────────────────────────────────


class TestIntentDetection:
    """Message keyword detection for recall, missions, etc."""

    # Recall
    def test_recall_show_memory(self):
        assert _wants_recall("show me a memory")

    def test_recall_remember_when(self):
        assert _wants_recall("Do you remember when we first met?")

    def test_recall_negative(self):
        assert not _wants_recall("What's the weather like?")

    def test_recall_case_insensitive(self):
        assert _wants_recall("SHOW ME A MEMORY")

    def test_recall_photo_album(self):
        assert _wants_recall("show me our photos")

    def test_recall_scrapbook(self):
        assert _wants_recall("open your scrapbook")

    # Mission start
    def test_mission_start_updates_every(self):
        assert _wants_mission_start("Give me updates every 30 minutes")

    def test_mission_start_report(self):
        assert _wants_mission_start("Report every hour")

    def test_mission_start_negative(self):
        assert not _wants_mission_start("Tell me about your day")

    # Mission cancel
    def test_mission_cancel_stop(self):
        assert _wants_mission_cancel("Stop updates")

    def test_mission_cancel_stand_down(self):
        assert _wants_mission_cancel("Stand down, Klukai")

    def test_mission_cancel_negative(self):
        assert not _wants_mission_cancel("What's the latest update?")


# ── Interval parsing ─────────────────────────────────────────────────────────


class TestParseInterval:
    def test_30_minutes(self):
        assert _parse_interval_minutes("update every 30 minutes") == 30

    def test_1_hour(self):
        assert _parse_interval_minutes("check in every 1 hour") == 60

    def test_every_hour(self):
        assert _parse_interval_minutes("report every hour") == 60

    def test_half_hour(self):
        assert _parse_interval_minutes("every half hour") == 30

    def test_minimum_5_minutes(self):
        assert _parse_interval_minutes("every 1 minute") == 5

    def test_default_30(self):
        assert _parse_interval_minutes("keep me posted") == 30

    def test_2_hours(self):
        assert _parse_interval_minutes("every 2 hours") == 120


# ── Squad member detection ────────────────────────────────────────────────────


class TestSquadDetection:
    def test_detect_mechty(self):
        from app.helpers import detect_squad_address
        assert detect_squad_address("Hey Mechty, come here") == "Mechty"

    def test_detect_by_alias(self):
        from app.helpers import detect_squad_address
        assert detect_squad_address("Where's G11?") == "Mechty"

    def test_detect_belka(self):
        from app.helpers import detect_squad_address
        assert detect_squad_address("How's Belka doing?") == "Belka"

    def test_detect_leva_by_ump45(self):
        from app.helpers import detect_squad_address
        assert detect_squad_address("Talk to UMP45") == "Leva"

    def test_no_squad_member(self):
        from app.helpers import detect_squad_address
        assert detect_squad_address("Hello Commander") is None

    def test_case_insensitive(self):
        from app.helpers import detect_squad_address
        assert detect_squad_address("WHERE IS ANDORIS") == "Andoris"


# ── Dream inquiry detection ───────────────────────────────────────────────────


class TestDreamDetection:
    def test_dream_inquiry_positive(self):
        from app.helpers import wants_dream_inquiry
        assert wants_dream_inquiry("Did you dream about me?")

    def test_dream_inquiry_sleep(self):
        from app.helpers import wants_dream_inquiry
        assert wants_dream_inquiry("How did you sleep?")

    def test_dream_inquiry_negative(self):
        from app.helpers import wants_dream_inquiry
        assert not wants_dream_inquiry("What's for dinner?")


# ── store_message: success/failure signalling (2026-06-11) ───────────────────


class TestStoreMessage:
    """store_message used to swallow every DB exception and return None — the
    chat path could not react to a lost message. It now reports success."""

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, patch

        from app.helpers import store_message

        conn = AsyncMock()

        @asynccontextmanager
        async def _ctx():
            yield conn

        with patch("app.db.get_conn", _ctx):
            ok = await store_message("conv-1", "user", "hello", user_id="alice")
        assert ok is True
        conn.commit.assert_awaited_once()
        # Message INSERT + conversation turn-count UPDATE both issued.
        sqls = [c.args[0] for c in conn.execute.call_args_list]
        assert any("INSERT INTO companion_messages" in s for s in sqls)
        assert any("UPDATE companion_conversations" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_returns_false_on_db_error(self):
        from unittest.mock import patch

        from app.helpers import store_message

        def broken():
            raise RuntimeError("db down")

        with patch("app.db.get_conn", side_effect=broken):
            ok = await store_message("conv-1", "user", "hello")
        assert ok is False
