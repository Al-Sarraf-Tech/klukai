"""Each model delta is forwarded as its own websocket token frame."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.chat_reply import ChatReplyRuntime
from app.models import SessionState


async def test_each_delta_is_sent_without_waiting_for_a_sentence():
    sent: list[str] = []

    async def stream(_system, _messages, _config):
        for piece in ("(I ", "look up.)", " Ready."):
            yield piece

    router = MagicMock()
    router.needs_agent = AsyncMock(return_value=False)
    router.route = AsyncMock(return_value=MagicMock(model="grok-4.7"))
    router.stream = stream
    ws = MagicMock()
    ws.send_token = AsyncMock(side_effect=lambda _uid, text: sent.append(text))
    ws.send_thinking = AsyncMock()
    ws.send_done = AsyncMock()
    runtime = ChatReplyRuntime(
        router,
        MagicMock(),
        ws,
        MagicMock(),
        lambda text: text.replace("(You ", "(I "),
        lambda text, n=8: [text],
        "...",
    )
    reply = await runtime.direct(
        "status",
        SessionState(conversation_id="conv-1"),
        "persona",
        [{"role": "user", "content": "status"}],
        "u",
        "m1",
        0.0,
    )
    assert sent == ["(I ", "look up.)", " Ready."]
    assert reply.text == "(I look up.) Ready."
