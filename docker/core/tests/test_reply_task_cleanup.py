"""Delayed status notifications belong to their reply, including cancellation."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.chat_reply import ChatReplyRuntime
from app.llm_router import LLMConfig, LLMStreamFailed
from app.models import SessionState


@pytest.mark.parametrize(
    "exit_path", ["cancel", "stream_error", "socket_error", "empty", "success"]
)
async def test_reply_settles_its_warmup_notifier_on_every_exit(monkeypatch, exit_path):
    entered, release = asyncio.Event(), asyncio.Event()
    notifications = []
    create_task = asyncio.create_task

    def track(coroutine, *args, **kwargs):
        task = create_task(coroutine, *args, **kwargs)
        if "_warmup_notify" in coroutine.__qualname__:
            notifications.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", track)

    class Router:
        async def route(self, *_args, **_kwargs):
            return LLMConfig(
                provider="anthropic", model="offline", base_url="http://127.0.0.1:1"
            )

        async def stream(self, *_args, **_kwargs):
            entered.set()
            await release.wait()
            if exit_path == "stream_error":
                raise LLMStreamFailed("offline fixture")
            if exit_path != "empty":
                yield "Ready."

    ws = AsyncMock()
    if exit_path == "socket_error":
        ws.send_token.side_effect = RuntimeError("offline socket closed")
    runtime = ChatReplyRuntime(
        Router(),
        None,
        ws,
        None,
        lambda value: value,
        lambda value, _size: [value],
        "Communications disrupted",
    )
    reply = create_task(
        runtime.direct(
            "hello",
            SessionState(conversation_id="fixture"),
            "system",
            [],
            "owner",
            "message",
            0,
        )
    )
    try:
        await entered.wait()
        assert len(notifications) == 1
        if exit_path == "cancel":
            reply.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reply
        else:
            release.set()
            if exit_path == "socket_error":
                with pytest.raises(RuntimeError, match="socket"):
                    await reply
            else:
                result = await reply
                assert result.failed is (exit_path in {"stream_error", "empty"})
        assert all(task.done() for task in notifications), (
            "A retired reply must not send a delayed status to a later connection"
        )
    finally:
        reply.cancel()
        for task in notifications:
            task.cancel()
        await asyncio.gather(reply, *notifications, return_exceptions=True)


@pytest.mark.parametrize("token_count", [1, 2])
async def test_reply_waits_for_started_notifier_finalizer(monkeypatch, token_count):
    started, cleanup_started = asyncio.Event(), asyncio.Event()
    cleanup_release, cleanup_finished = asyncio.Event(), asyncio.Event()
    actual_sleep = asyncio.sleep

    async def quick_sleep(delay):
        await actual_sleep(0 if delay == 2.5 else delay)

    monkeypatch.setattr(asyncio, "sleep", quick_sleep)

    class Socket:
        async def send_thinking(self, _user_id, text):
            if text.startswith("One moment"):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await cleanup_release.wait()
                    cleanup_finished.set()

        async def send_token(self, *_args):
            await cleanup_started.wait()

        async def send_done(self, *_args, **_kwargs):
            pass

    class Router:
        async def route(self, *_args, **_kwargs):
            return LLMConfig(
                provider="anthropic", model="offline", base_url="http://127.0.0.1:1"
            )

        async def stream(self, *_args, **_kwargs):
            await started.wait()
            for _ in range(token_count):
                yield "Ready."

    runtime = ChatReplyRuntime(
        Router(), None, Socket(), None, lambda value: value, None, "failure"
    )
    reply = asyncio.create_task(
        runtime.direct(
            "hello", SessionState(conversation_id="fixture"), "system", [], "u", "m", 0
        )
    )
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(reply), timeout=0.02)
        cleanup_release.set()
        await asyncio.wait_for(reply, timeout=1)
        assert cleanup_finished.is_set(), (
            "Reply must join asynchronous notifier cleanup"
        )
    finally:
        cleanup_release.set()
        await asyncio.gather(reply, return_exceptions=True)
