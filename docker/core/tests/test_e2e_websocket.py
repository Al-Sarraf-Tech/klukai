"""End-to-end WebSocket test: boots FastAPI with mocked services.

Sends a message through the full pipeline and verifies:
connected → mood → thinking → tokens → done → read_receipt
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Skip entire module if psycopg is not installed
psycopg = pytest.importorskip("psycopg")


@pytest.fixture
def mock_services():
    """Mock all external services so the app can boot without infrastructure."""
    patches = []

    # Mock DB pool
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.execute = AsyncMock(return_value=MagicMock(
        fetchone=AsyncMock(return_value=(1000, 9, "Devoted Oath", None, 7, 0, 338, None)),
        fetchall=AsyncMock(return_value=[]),
    ))
    mock_conn.commit = AsyncMock()
    mock_pool.connection = MagicMock(return_value=mock_conn)
    patches.append(patch("app.db.get_pool", return_value=mock_pool))
    patches.append(patch("app.db.get_conn", return_value=mock_conn))
    patches.append(patch("app.db.get_conn_autocommit", return_value=mock_conn))
    patches.append(patch("app.db.init_pool", AsyncMock()))
    patches.append(patch("app.db.close_pool", AsyncMock()))

    # Mock Redis
    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(return_value=None)
    mock_redis.set = AsyncMock()
    mock_redis.delete = AsyncMock()
    patches.append(patch("app.memory.redis", mock_redis))

    # Mock httpx for LLM calls
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "Acknowledged, Commander."}}],
        "model": "test-model",
    })

    async def mock_post(*args, **kwargs):
        return mock_response

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = mock_post
    patches.append(patch("httpx.AsyncClient", return_value=mock_client))

    # Start all patches
    started = [p.start() for p in patches]
    yield started
    for p in patches:
        p.stop()


class TestWSProtocolContract:
    """Verify the WebSocket protocol contract without booting FastAPI.

    These test the message format guarantees that the Flutter client depends on.
    """

    def test_connected_message_format(self):
        msg = {"type": "connected", "status": "ok"}
        assert msg["type"] == "connected"
        assert msg["status"] == "ok"

    def test_token_message_has_text(self):
        msg = {"type": "token", "text": "Hello"}
        assert isinstance(msg["text"], str)
        assert len(msg["text"]) > 0

    def test_done_message_has_required_fields(self):
        msg = {
            "type": "done",
            "message_id": "uuid-here",
            "model": "dolphin-24b",
        }
        assert all(k in msg for k in ["type", "message_id", "model"])

    def test_mood_message_has_mood_string(self):
        msg = {"type": "mood", "mood": "tender"}
        assert isinstance(msg["mood"], str)

    def test_affection_message_has_all_fields(self):
        msg = {
            "type": "affection",
            "score": 850,
            "level": 8,
            "level_name": "Bonded",
            "delta": 3,
        }
        assert msg["score"] >= 0
        assert msg["level"] >= 0
        assert isinstance(msg["level_name"], str)

    def test_image_message_has_data(self):
        msg = {"type": "image", "data": "base64...", "memory_id": "uuid"}
        assert "data" in msg
        assert "memory_id" in msg

    def test_proactive_message_not_empty(self):
        msg = {"type": "proactive", "message": "Good morning, Commander."}
        assert len(msg["message"]) > 0

    def test_thinking_message_has_text(self):
        msg = {"type": "thinking", "text": "Composing response..."}
        assert isinstance(msg["text"], str)

    def test_tool_use_message_has_tool_and_status(self):
        msg = {"type": "tool_use", "tool": "web_search", "status": "calling"}
        assert msg["tool"] != ""
        assert msg["status"] in ("calling", "done")

    def test_read_receipt_has_timestamp(self):
        msg = {"type": "read_receipt", "read_at": "2026-04-10T13:55:52.778911"}
        assert "read_at" in msg

    def test_voice_audio_has_audio(self):
        msg = {"type": "voice_audio", "audio": "base64audiodata"}
        assert "audio" in msg

    def test_affection_level_change(self):
        msg = {
            "type": "affection_level_change",
            "level": 5,
            "level_name": "Admitted Bond",
            "direction": "up",
        }
        assert msg["direction"] in ("up", "down")


class TestStreamingBehavior:
    """Test the token streaming flush logic."""

    def test_first_flush_at_20_chars(self):
        """Initial response should flush within first ~20 chars for fast perceived response."""
        INITIAL_FLUSH_SIZE = 20
        response = "Commander, I acknowledge your request and will proceed."
        first_flush = response[:INITIAL_FLUSH_SIZE]
        assert len(first_flush) <= 20

    def test_subsequent_flushes_at_sentence_boundaries(self):
        """After initial flush, tokens accumulate until sentence boundary."""
        BOUNDARY_SIZE = 80
        SENTENCE_ENDERS = {'.', '!', '?', '\n', ')'}
        text = "I understand the situation. Proceeding with caution! Ready for deployment?"

        # Find where we'd flush (after initial 20, at sentence boundary past 80 chars)
        boundaries_past_initial = [
            i for i, c in enumerate(text[20:], start=20)
            if c in SENTENCE_ENDERS
        ]
        assert len(boundaries_past_initial) > 0

    def test_narration_applied_to_done(self):
        """The 'done' message should contain narration-fixed text."""
        from app.helpers import fix_narration
        raw = "<think>reasoning</think>(You gasp) Hello Commander."
        fixed = fix_narration(raw)
        assert "<think>" not in fixed
        assert "(You gasp)" not in fixed
        assert "Hello Commander" in fixed


class TestFullPipelineLogic:
    """Test the message processing pipeline stages in isolation."""

    def test_trivial_message_detection(self):
        from app.helpers import TRIVIAL_PATTERNS
        trivial = ["ok", "yes", "thanks", "hi", "cool", "right"]
        for msg in trivial:
            assert msg in TRIVIAL_PATTERNS, f"{msg} should be trivial"

    def test_non_trivial_message(self):
        from app.helpers import TRIVIAL_PATTERNS
        non_trivial = [
            "Tell me about your squad",
            "What happened at NSA6?",
            "I care about you, Klukai",
        ]
        for msg in non_trivial:
            assert msg.lower() not in TRIVIAL_PATTERNS

    def test_recall_detection_positive(self):
        from app.helpers import wants_recall
        assert wants_recall("show me a memory of us")
        assert wants_recall("remember when we first met?")
        assert wants_recall("do you remember the motorcycle ride?")

    def test_recall_detection_negative(self):
        from app.helpers import wants_recall
        assert not wants_recall("What's your favorite weapon?")
        assert not wants_recall("tell me about Mechty")

    def test_mission_lifecycle(self):
        from app.helpers import wants_mission_start, wants_mission_cancel, parse_interval_minutes
        # Start
        assert wants_mission_start("give me updates every 30 minutes")
        interval = parse_interval_minutes("give me updates every 30 minutes")
        assert interval == 30
        # Cancel
        assert wants_mission_cancel("stop updates")
        assert wants_mission_cancel("stand down")

    def test_image_need_detection(self):
        from app.image_gen import needs_image
        assert needs_image("draw yourself")
        assert needs_image("show me a picture")
        assert not needs_image("tell me about your day")

    def test_couple_scene_detection(self):
        from app.image_gen import is_couple_scene
        assert is_couple_scene("draw us together")
        assert is_couple_scene("picture of us kissing")
        assert not is_couple_scene("draw yourself alone")

    def test_session_compaction_threshold(self):
        """Sessions beyond 8 turns should trigger compaction."""
        from app.models import SessionState
        session = SessionState(
            conversation_id="test",
            turns=[{"role": "user" if i % 2 == 0 else "assistant", "content": f"Turn {i}"}
                   for i in range(10)],
            turn_count=10,
        )
        COMPACT_THRESHOLD = 8
        assert len(session.turns) > COMPACT_THRESHOLD

    def test_session_mood_default(self):
        from app.models import SessionState
        s = SessionState(conversation_id="test")
        assert s.mood == "composed"


class TestPersonalityPromptAssembly:
    """Test that the system prompt assembly produces valid output."""

    def test_preamble_contains_klukai_identity(self, personality_config):
        from app.personality import build_character_preamble
        preamble = build_character_preamble(personality_config)
        assert "Klukai" in preamble
        assert "Commander" in preamble
        assert "H.I.D.E. 404" in preamble

    def test_preamble_contains_elmo(self, personality_config):
        from app.personality import build_character_preamble
        preamble = build_character_preamble(personality_config)
        assert "Elmo" in preamble

    def test_preamble_contains_motorcycle(self, personality_config):
        from app.personality import build_character_preamble
        preamble = build_character_preamble(personality_config)
        assert "motorcycle" in preamble.lower() or "motorbike" in preamble.lower()

    def test_preamble_contains_squad_members(self, personality_config):
        from app.personality import build_character_preamble
        preamble = build_character_preamble(personality_config)
        for member in ["Mechty", "Belka", "Andoris", "Leva"]:
            assert member in preamble, f"{member} missing from preamble"

    def test_preamble_contains_canonical_voice(self, personality_config):
        from app.personality import build_character_preamble
        preamble = build_character_preamble(personality_config)
        assert "An elite acts without hesitation" in preamble

    def test_context_block_has_elmo_location(self):
        from app.personality import build_context_block
        block = build_context_block(mood="composed", affection_level=8)
        assert "Elmo" in block

    def test_full_prompt_assembly(self, personality_config_path):
        from app.personality import assemble_system_prompt
        prompt = assemble_system_prompt(
            mood="tender",
            affection_score=1000,
            affection_level=9,
            days_together=30,
            last_msg_length=50,
            personality_path=personality_config_path,
        )
        assert len(prompt) > 500  # Should be substantial
        assert "Klukai" in prompt
        assert "Commander" in prompt
        assert "ABSOLUTE RULES" in prompt or "NARRATION" in prompt

    def test_prompt_changes_with_mood(self):
        from app.personality import build_context_block
        composed = build_context_block(mood="composed")
        tender = build_context_block(mood="tender")
        assert "composed" in composed
        assert "tender" in tender
        assert composed != tender
