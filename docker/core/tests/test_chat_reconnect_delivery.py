"""A live reply must finish correctly on a device that missed its prefix."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent_loop import AgentResult
from app.chat_reply import ChatReplyRuntime
from app.helpers import chunk_text, fix_narration
from app.llm_router import FAILURE_SENTINEL
from app.models import LLMConfig, SessionState
from app.ws_manager import WSManager


class Device:
    def __init__(self, first_token=None, release=None):
        self.output = []
        self.accept = AsyncMock()
        self.close = AsyncMock()
        self.first_token = first_token
        self.release = release

    async def send_text(self, payload):
        event = json.loads(payload)
        self.output.append(event)
        if event["type"] == "token" and self.first_token is not None:
            self.first_token.set()
            await self.release.wait()
            self.first_token = None


@pytest.mark.parametrize("route", ["direct", "agent"])
@pytest.mark.parametrize("churn", ["second-device", "reconnect"])
async def test_device_joining_mid_reply_receives_complete_final_text(route, churn):
    manager = WSManager()
    first_token, release = asyncio.Event(), asyncio.Event()
    original = Device(first_token, release)
    late = Device()
    await manager.connect(original, "alice")
    text = "The first part was already delivered. Here is the rest."
    router = MagicMock()
    router.needs_agent = AsyncMock(return_value=route == "agent")
    router.route = AsyncMock(
        return_value=LLMConfig(provider="offline-test", model="test-model")
    )

    async def stream(*args):
        yield "The first part was already delivered."
        yield " Here is the rest."

    router.stream = stream
    agent = MagicMock()
    agent.run = AsyncMock(return_value=AgentResult(response=text, model="test-model"))
    runtime = ChatReplyRuntime(
        router,
        MagicMock(),
        manager,
        MagicMock(return_value=agent),
        fix_narration,
        chunk_text,
        FAILURE_SENTINEL.split(",", 1)[0],
    )
    task = asyncio.create_task(
        runtime.respond(
            "hello",
            SessionState(conversation_id="c"),
            "system",
            [],
            "alice",
            "reply-id",
            time.monotonic(),
        )
    )
    try:
        await asyncio.wait_for(first_token.wait(), 1)
        if churn == "reconnect":
            await manager.disconnect("alice", original)
        await manager.connect(late, "alice")
        release.set()
        reply = await asyncio.wait_for(task, 1)
        tail = "".join(
            event["text"] for event in late.output if event["type"] == "token"
        )
        assert tail and tail != text, (
            "fixture must actually omit the already-delivered prefix"
        )
        done = [event for event in late.output if event["type"] == "done"]
        assert len(done) == 1
        assert done[0].get("text") == text, (
            "late device can only finalize a truncated reply"
        )
        assert done[0]["message_id"] == "reply-id"
        assert reply.text == text and not reply.failed
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()


@pytest.mark.parametrize("failure", ["stream-error", "cancel"])
async def test_interrupted_direct_reply_does_not_publish_incomplete_final_text(failure):
    from app.llm_router import LLMStreamFailed

    manager = WSManager()
    device = Device()
    await manager.connect(device, "alice")
    router = MagicMock()
    router.route = AsyncMock(
        return_value=LLMConfig(provider="offline-test", model="test-model")
    )
    entered = asyncio.Event()

    async def stream(*args):
        yield "Already visible."
        yield " hidden-tail"
        if failure == "cancel":
            entered.set()
            await asyncio.Event().wait()
        raise LLMStreamFailed("offline fixture stream failed")

    router.stream = stream
    runtime = ChatReplyRuntime(
        router,
        MagicMock(),
        manager,
        MagicMock(),
        fix_narration,
        chunk_text,
        FAILURE_SENTINEL.split(",", 1)[0],
    )
    task = asyncio.create_task(
        runtime.direct(
            "hello",
            SessionState(conversation_id="c"),
            "system",
            [],
            "alice",
            "reply-id",
            time.monotonic(),
        )
    )
    try:
        if failure == "cancel":
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not any(event["type"] == "done" for event in device.output)
        else:
            result = await asyncio.wait_for(task, 1)
            assert result.failed
            assert [event["type"] for event in device.output][-2:] == ["error", "done"]
            assert "text" not in device.output[-1]
        assert [
            event["text"] for event in device.output if event["type"] == "token"
        ] == ["Already visible."]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()


async def test_cancelled_agent_delivery_does_not_emit_done():
    manager = WSManager()
    first_token, release = asyncio.Event(), asyncio.Event()
    device = Device(first_token, release)
    await manager.connect(device, "alice")
    agent = MagicMock()
    agent.run = AsyncMock(
        return_value=AgentResult(response="First words. More will not be delivered.")
    )
    runtime = ChatReplyRuntime(
        MagicMock(),
        MagicMock(),
        manager,
        MagicMock(return_value=agent),
        fix_narration,
        chunk_text,
        FAILURE_SENTINEL.split(",", 1)[0],
    )
    task = asyncio.create_task(
        runtime.agent("system", [], "alice", "reply-id", time.monotonic())
    )
    try:
        await asyncio.wait_for(first_token.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not any(event["type"] == "done" for event in device.output)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()


@pytest.mark.parametrize("route", ["direct", "agent"])
@pytest.mark.parametrize("repetitions", [1, 1024], ids=["short", "long-unicode"])
async def test_final_frame_matches_canonical_persisted_reply(route, repetitions):
    from app import chat_handlers
    from tests.test_chat_handlers_coverage import _patched_pipeline

    text = ("Status nominal, Commander. 🙂 " * repetitions).strip()
    raw = "<think>internal planning</think>" + text
    manager = WSManager()
    device = Device()
    await manager.connect(device, "alice")
    state = SessionState(conversation_id="c")
    with (
        _patched_pipeline(
            provider="offline-test",
            needs_agent=route == "agent",
            agent_response=raw,
            stream_tokens=[raw],
        ) as pipeline,
        patch.object(chat_handlers, "ws", manager),
        patch.object(chat_handlers, "_fix_narration", fix_narration),
    ):
        try:
            result = await chat_handlers._handle_message("hello", state, "alice")
            done = [event for event in device.output if event["type"] == "done"]
            assert len(done) == 1
            assert done[0]["text"] == text == result
            assert "<think>" not in done[0]["text"]
            assistant = next(
                call
                for call in pipeline.store_message.await_args_list
                if call.args[1] == "assistant"
            )
            assert assistant.args[2] == done[0]["text"]
            assert done[0]["message_id"], (
                "completion keeps its existing reply identifier"
            )
            assert state.turns[-1]["content"] == done[0]["text"]
            if repetitions > 1:
                assert len(json.dumps(done[0]).encode()) > 40_000
        finally:
            await manager.close()
