"""A completed tool answer is delivered without a second typing delay."""

import time
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent_loop import AgentResult
from app.chat_reply import ChatReplyRuntime
from app.helpers import fix_narration
from app.llm_router import FAILURE_SENTINEL


@pytest.mark.parametrize(
    "text",
    ["Ready.", "A complete answer. " * 150, "日本語の返事。"],
    ids=["short", "long", "unicode"],
)
async def test_completed_agent_answer_is_delivered_in_one_frame(text):
    events = []
    agent = Mock()
    agent.run = AsyncMock(return_value=AgentResult(response=text, model="test"))
    ws = AsyncMock()
    ws.send_token.side_effect = lambda user, token: events.append(("token", token))
    ws.send_done.side_effect = lambda *args, **kwargs: events.append(
        ("done", kwargs["final_text"])
    )
    runtime = ChatReplyRuntime(
        None,
        None,
        ws,
        Mock(return_value=agent),
        fix_narration,
        FAILURE_SENTINEL.split(",", 1)[0],
        on_completed=lambda reply: events.append(("completed", reply.text)),
    )
    reply = await runtime.agent("persona", [], "owner", "message", time.monotonic())
    expected = fix_narration(text)
    assert events == [("token", expected), ("completed", expected), ("done", expected)]
    assert reply.text == expected
    assert not reply.failed


@pytest.mark.parametrize("text", ["", FAILURE_SENTINEL])
async def test_failed_agent_answer_never_becomes_canonical(text):
    agent = Mock()
    agent.run = AsyncMock(return_value=AgentResult(response=text, model="test"))
    completed = Mock()
    ws = AsyncMock()
    runtime = ChatReplyRuntime(
        None,
        None,
        ws,
        Mock(return_value=agent),
        fix_narration,
        FAILURE_SENTINEL.split(",", 1)[0],
        on_completed=completed,
    )
    reply = await runtime.agent("persona", [], "owner", "message", time.monotonic())
    assert reply.failed
    completed.assert_not_called()
    ws.send_done.assert_awaited_once_with("owner", "message", "test", final_text=None)
