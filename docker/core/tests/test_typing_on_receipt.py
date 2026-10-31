"""Typing is shown when the message arrives, before the turn is admitted."""

from __future__ import annotations

from unittest.mock import AsyncMock

from app import chat


async def test_typing_precedes_session_work(monkeypatch):
    order: list[str] = []

    async def admit(*_args, **_kwargs):
        order.append("admit")

    async def thinking(_user_id, text):
        order.append(text)

    monkeypatch.setattr(chat, "_admit_turn", admit)
    monkeypatch.setattr(chat.ws, "send_thinking", thinking)
    await chat._dispatch_device_frame(
        {"type": "message", "content": "hello"}, "u", object()
    )
    assert order == ["…", "admit"]


async def test_an_empty_message_does_not_show_typing(monkeypatch):
    thinking = AsyncMock()
    monkeypatch.setattr(chat.ws, "send_thinking", thinking)
    monkeypatch.setattr(chat, "_reject_device_frame", AsyncMock())
    await chat._dispatch_device_frame(
        {"type": "message", "content": "   "}, "u", object()
    )
    thinking.assert_not_awaited()
