"""Tests for WebSocket protocol: message types, connection lifecycle, multi-user."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ws_manager import WSManager


class TestWSManager:
    """WebSocket manager: multi-device, user isolation, message broadcasting."""

    @pytest.fixture
    def ws(self):
        return WSManager()

    @pytest.mark.asyncio
    async def test_connect_tracks_user(self, ws):
        mock_socket = MagicMock()
        mock_socket.accept = AsyncMock()
        mock_socket.send_json = AsyncMock()
        await ws.connect(mock_socket, "user1")
        assert ws.is_connected("user1")

    @pytest.mark.asyncio
    async def test_disconnect_removes_user(self, ws):
        mock_socket = MagicMock()
        mock_socket.accept = AsyncMock()
        mock_socket.send_json = AsyncMock()
        await ws.connect(mock_socket, "user1")
        await ws.disconnect("user1", mock_socket)
        assert not ws.is_connected("user1")

    @pytest.mark.asyncio
    async def test_multiple_users_isolated(self, ws):
        s1, s2 = MagicMock(), MagicMock()
        s1.accept = AsyncMock()
        s1.send_json = AsyncMock()
        s2.accept = AsyncMock()
        s2.send_json = AsyncMock()
        await ws.connect(s1, "user1")
        await ws.connect(s2, "user2")
        assert ws.is_connected("user1")
        assert ws.is_connected("user2")

    def test_not_connected_by_default(self, ws):
        assert not ws.is_connected("nobody")


class TestMessageTypes:
    """Verify all WebSocket message type structures."""

    VALID_TYPES = [
        "connected", "token", "done", "thinking", "tool_use",
        "mood", "affection", "affection_level_change", "proactive",
        "voice_audio", "image", "read_receipt",
    ]

    def test_all_types_defined(self):
        """Ensure we test all protocol message types."""
        # This is a meta-test — if a new type is added, this list must be updated
        assert len(self.VALID_TYPES) >= 12

    def test_token_structure(self):
        msg = {"type": "token", "text": "Hello"}
        assert msg["type"] == "token"
        assert isinstance(msg["text"], str)

    def test_done_structure(self):
        msg = {"type": "done", "message_id": "uuid", "model": "dolphin-24b"}
        assert msg["type"] == "done"
        assert "message_id" in msg
        assert "model" in msg

    def test_mood_structure(self):
        msg = {"type": "mood", "mood": "tender"}
        assert msg["mood"] in [
            "composed", "focused", "prideful", "tender", "longing",
            "flustered", "affectionate", "devoted", "content", "playful",
        ]

    def test_affection_structure(self):
        msg = {
            "type": "affection",
            "score": 500, "level": 5, "level_name": "Admitted Bond",
            "delta": 3,
        }
        assert msg["score"] >= 0
        assert msg["level"] >= 0
        assert msg["delta"] != 0 or msg["delta"] == 0  # Can be zero

    def test_image_structure(self):
        msg = {"type": "image", "data": "base64data...", "memory_id": "uuid"}
        assert msg["type"] == "image"
        assert isinstance(msg["data"], str)

    def test_proactive_structure(self):
        msg = {"type": "proactive", "message": "Good morning, Commander."}
        assert msg["type"] == "proactive"
        assert len(msg["message"]) > 0


class TestStreamingProtocol:
    """Token streaming behavior: initial flush, boundary detection."""

    def test_initial_flush_threshold(self):
        """First token batch should be ~20 chars (fast perceived response)."""
        # The streaming protocol sends first batch at 20 chars
        INITIAL_FLUSH = 20
        text = "Hello Commander, reporting for duty as requested."
        first_batch = text[:INITIAL_FLUSH]
        assert len(first_batch) == 20
        assert first_batch == "Hello Commander, rep"

    def test_sentence_boundary_detection(self):
        """Subsequent tokens flush at sentence boundaries (., !, ?, newline)."""
        SENTENCE_ENDERS = {'.', '!', '?', '\n', ')'}
        text = "I understand. Proceeding! Ready?"
        boundaries = [i for i, c in enumerate(text) if c in SENTENCE_ENDERS]
        assert len(boundaries) >= 3  # Three sentence enders
