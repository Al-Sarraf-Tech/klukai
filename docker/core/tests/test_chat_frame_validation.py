"""Malformed frames must not acquire a turn and invalidate pending extraction."""

import json
from unittest.mock import AsyncMock

import pytest

from app import chat
from app.ws_manager import WSManager


class Device:
    def __init__(self):
        self.output = []
        self.accept = AsyncMock()
        self.close = AsyncMock()

    async def send_text(self, payload):
        self.output.append(json.loads(payload))


@pytest.fixture
async def devices(monkeypatch):
    manager = WSManager()
    sender, other = Device(), Device()
    await manager.connect(sender, "alice")
    await manager.connect(other, "alice")
    sender.output.clear()
    other.output.clear()
    monkeypatch.setattr(chat, "ws", manager)
    yield sender, other
    await manager.close()


@pytest.mark.parametrize(
    "content",
    [None, 42, [], {}, "", " \n\t", " " * 4000 + "hello"],
    ids=["null", "number", "array", "object", "empty", "whitespace", "truncated-empty"],
)
async def test_invalid_text_does_not_admit_turn(monkeypatch, devices, content):
    admit = AsyncMock()
    sender, other = devices
    monkeypatch.setattr(chat, "_admit_turn", admit)
    await chat._dispatch_device_frame(
        {"type": "message", "content": content}, "alice", sender
    )
    admit.assert_not_awaited()
    assert sender.output[-1]["type"] == "error"
    assert other.output == []


@pytest.mark.parametrize(
    "audio",
    [42, ["audio"], {"audio": "data"}, "x" * 8_000_001],
    ids=["number", "array", "object", "oversized"],
)
async def test_invalid_voice_does_not_admit_turn(monkeypatch, devices, audio):
    admit = AsyncMock()
    sender, other = devices
    monkeypatch.setattr(chat, "_admit_turn", admit)
    await chat._dispatch_device_frame(
        {"type": "voice_end", "audio": audio}, "alice", sender
    )
    assert admit.await_count == 0
    assert sender.output[-1]["type"] == "error"
    assert other.output == []
