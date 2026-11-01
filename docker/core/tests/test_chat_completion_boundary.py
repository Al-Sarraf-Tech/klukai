"""Offline completed-reply cancellation regressions with real turn ownership."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app import chat, chat_handlers
from app.memory import MemoryManager
from app.models import SessionState
from app.ws_manager import WSManager
from tests.test_chat_handlers_coverage import _patched_pipeline
from tests.test_session_authority import StoredRedis


@pytest.mark.parametrize("route", ["direct", "agent"])
@pytest.mark.parametrize(
    "done_position", [1, 2], ids=["before-first-done", "between-done-sends"]
)
@pytest.mark.parametrize("prior_pairs", [1, 8], ids=["short-context", "full-context"])
async def test_cancelled_done_keeps_completed_reply_history_and_live_context(
    monkeypatch,
    route,
    done_position,
    prior_pairs,
):
    storage = MemoryManager()
    storage._redis = StoredRedis()
    sid = chat.session_id("alice")
    previous = [
        {"role": role, "content": f"previous {role} {index}"}
        for index in range(prior_pairs)
        for role in ("user", "assistant")
    ]
    await storage.save_session(
        sid,
        SessionState(
            conversation_id="c",
            turns=previous,
            turn_count=len(previous),
        ),
    )
    manager = WSManager()
    blocked, release = asyncio.Event(), asyncio.Event()
    done_calls, deliveries, history = [], [], []

    class Device:
        accept = AsyncMock()
        close = AsyncMock()

        async def send_text(self, payload):
            frame = json.loads(payload)
            if frame["type"] == "done":
                done_calls.append(frame)
                if len(done_calls) == done_position:
                    blocked.set()
                    await release.wait()
                deliveries.append(frame)

    async def store(conversation_id, role, content, *args, **kwargs):
        history.append((role, content))
        return True

    await manager.connect(Device(), "alice")
    await manager.connect(Device(), "alice")
    with _patched_pipeline(
        provider="offline-test",
        needs_agent=route == "agent",
        stream_tokens=["Complete answer."],
        agent_response="Complete answer.",
    ) as pipeline:
        pipeline.memory.add_turn.side_effect = storage.add_turn
        pipeline.memory.save_session.side_effect = storage.save_session
        pipeline.store_message.side_effect = store
        monkeypatch.setattr(
            chat_handlers.ChatPromptBuilder, "build", AsyncMock(return_value="system")
        )
        monkeypatch.setattr(chat, "memory", storage)
        monkeypatch.setattr(chat, "ws", manager)
        monkeypatch.setattr(chat, "_handle_message", chat_handlers._handle_message)
        monkeypatch.setattr(chat_handlers, "ws", manager)
        task = asyncio.create_task(
            chat._admit_turn("message", "Current question", "alice")
        )
        try:
            await asyncio.wait_for(blocked.wait(), 1)
            assert len(deliveries) == done_position - 1
            task.cancel("original cancellation")
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            assert history == [
                ("user", "Current question"),
                ("assistant", "Complete answer."),
            ], (
                "a fully generated reply must not disappear when its done broadcast is cancelled"
            )
            current = await storage.get_session(sid)
            expected = previous + [
                {"role": "user", "content": "Current question"},
                {"role": "assistant", "content": "Complete answer."},
            ]
            from app.conversation_continuity import cached_history

            assert current.turns == cached_history(expected)
            assert current.turn_count == len(previous) + 2
            assert current.revision == 1
            assert not storage._session_locks
            pipeline.background_extraction.assert_not_called()
            pipeline.background_compaction.assert_not_called()
            pipeline.background_image_gen.assert_not_called()
            assert len(done_calls) == done_position, (
                "no detached terminal delivery may continue"
            )
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await manager.close()


@pytest.mark.parametrize("route", ["direct", "agent"])
@pytest.mark.parametrize(
    "stage",
    ["db-before-ack", "db-after-commit", "redis-before-ack", "redis-after-write"],
)
@pytest.mark.parametrize(
    "cancel_at_done", [False, True], ids=["cancel-storage", "cancel-done"]
)
async def test_repeated_cancellation_joins_one_commit_before_next_device(
    monkeypatch,
    route,
    stage,
    cancel_at_done,
):
    from contextlib import asynccontextmanager
    from app import db
    from app.conversation_store import store_message

    real_sleep = asyncio.sleep
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    storage = MemoryManager()
    sid = chat.session_id("alice")
    history, inserts, frames = [], [], []
    done_entered = asyncio.Event()
    gated = False

    async def gate():
        nonlocal gated
        gated = True
        entered.set()
        try:
            await release.wait()
        finally:
            finished.set()

    class Redis(StoredRedis):
        async def set(self, key, value, **kwargs):
            assistant = json.loads(value)["turns"][-1:]
            should_gate = (
                stage.startswith("redis")
                and not gated
                and assistant
                and assistant[0]["role"] == "assistant"
            )
            if should_gate and stage == "redis-before-ack":
                await gate()
            result = await super().set(key, value, **kwargs)
            if should_gate and stage == "redis-after-write":
                await gate()
            return result

    class Transaction:
        pending = None

        async def execute(self, sql, args):
            if sql.startswith("INSERT INTO companion_messages"):
                self.pending = (args[1], args[2])
                inserts.append(self.pending)

        async def commit(self):
            should_gate = (
                self.pending[0] == "assistant" and stage.startswith("db") and not gated
            )
            if should_gate and stage == "db-before-ack":
                await gate()
            history.append(self.pending)
            if should_gate and stage == "db-after-commit":
                await gate()

    @asynccontextmanager
    async def connection():
        yield Transaction()

    class Device:
        accept = AsyncMock()
        close = AsyncMock()

        async def send_text(self, payload):
            frame = json.loads(payload)
            frames.append(frame)
            if cancel_at_done and frame["type"] == "done" and not done_entered.is_set():
                done_entered.set()
                await asyncio.Event().wait()

    storage._redis = Redis()
    await storage.save_session(sid, SessionState(conversation_id="c"))
    manager = WSManager()
    await manager.connect(Device(), "alice")
    with _patched_pipeline(
        provider="offline-test",
        needs_agent=route == "agent",
        stream_tokens=["Complete answer."],
        agent_response="Complete answer.",
    ) as pipeline:
        pipeline.memory.add_turn.side_effect = storage.add_turn
        pipeline.memory.save_session.side_effect = storage.save_session
        pipeline.store_message.side_effect = store_message
        build = AsyncMock(return_value="system")
        monkeypatch.setattr(chat_handlers.ChatPromptBuilder, "build", build)
        monkeypatch.setattr(db, "get_conn", connection)
        monkeypatch.setattr(chat, "memory", storage)
        monkeypatch.setattr(chat, "ws", manager)
        monkeypatch.setattr(chat, "_handle_message", chat_handlers._handle_message)
        monkeypatch.setattr(chat_handlers, "ws", manager)
        first = asyncio.create_task(
            chat._admit_turn("message", "First question", "alice")
        )
        second = None
        try:
            if cancel_at_done:
                await asyncio.wait_for(done_entered.wait(), 1)
                first.cancel("original cancellation")
            await asyncio.wait_for(entered.wait(), 1)
            assert len([f for f in frames if f["type"] == "done"]) == 1
            if not cancel_at_done:
                first.cancel("original cancellation")
            await real_sleep(0)
            second = asyncio.create_task(
                chat._admit_turn("message", "Next question", "alice")
            )
            for _ in range(2):
                first.cancel("repeated cancellation")
                await real_sleep(0)
            assert not first.done(), "cancelled turn released its unfinished finalizer"
            assert not second.done()
            assert build.await_count == 1, (
                "next device entered before completed reply finalized"
            )
            assert sid in storage._session_locks
            assert not finished.is_set(), (
                "cancellation reached the retained storage operation"
            )
            pipeline.background_extraction.assert_not_called()
            pipeline.background_image_gen.assert_not_called()
            release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await first
            assert error.value.args == ("original cancellation",)
            await asyncio.wait_for(second, 1)
            assert finished.is_set()
            assert (
                inserts
                == history
                == [
                    ("user", "First question"),
                    ("assistant", "Complete answer."),
                    ("user", "Next question"),
                    ("assistant", "Complete answer."),
                ]
            )
            state = await storage.get_session(sid)
            assert state.turns == [
                {"role": role, "content": content} for role, content in history
            ]
            assert state.revision == 2
            assert state.turn_count == 4
            done = [f for f in frames if f["type"] == "done"]
            assert len(done) == 2 and done[0]["message_id"] != done[1]["message_id"]
            assert not storage._session_locks
        finally:
            release.set()
            tasks = [first] + ([second] if second is not None else [])
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await manager.close()


@pytest.mark.parametrize("route", ["direct", "agent"])
@pytest.mark.parametrize("stage", ["provider", "token-delivery"])
async def test_cancellation_before_completion_never_persists_an_answer(
    monkeypatch, route, stage
):
    storage = MemoryManager()
    storage._redis = StoredRedis()
    sid = chat.session_id("alice")
    previous = [{"role": role, "content": role} for role in ("user", "assistant")]
    await storage.save_session(
        sid, SessionState(conversation_id="c", turns=previous, turn_count=2)
    )
    entered = asyncio.Event()

    async def pause(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def stream(*args):
        await pause()
        yield "never completed"

    with _patched_pipeline(
        provider="offline-test",
        needs_agent=route == "agent",
    ) as pipeline:
        pipeline.memory.add_turn.side_effect = storage.add_turn
        pipeline.memory.save_session.side_effect = storage.save_session
        monkeypatch.setattr(
            chat_handlers.ChatPromptBuilder, "build", AsyncMock(return_value="system")
        )
        monkeypatch.setattr(chat, "memory", storage)
        monkeypatch.setattr(chat, "ws", pipeline.ws)
        monkeypatch.setattr(chat, "_handle_message", chat_handlers._handle_message)
        if stage == "provider":
            pipeline.router.stream = stream
            pipeline.agent.run.side_effect = pause
        else:
            pipeline.ws.send_token.side_effect = pause
        task = asyncio.create_task(
            chat._admit_turn("message", "Current question", "alice")
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel("before completion")
            with pytest.raises(asyncio.CancelledError):
                await task
            pipeline.store_message.assert_awaited_once()
            assert pipeline.store_message.await_args.args[1] == "user"
            assert (await storage.get_session(sid)).turns == previous
            pipeline.ws.send_done.assert_not_called()
            pipeline.background_extraction.assert_not_called()
            assert not storage._session_locks
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("route", ["direct", "agent"])
@pytest.mark.parametrize(
    "mode",
    [
        "db-false",
        "db-timeout",
        "db-uncertain-timeout",
        "redis-failure",
        "redis-timeout",
        "redis-uncertain-timeout",
    ],
)
async def test_cancelled_storage_failure_keeps_cancellation_and_leaves_no_writer(
    monkeypatch,
    route,
    mode,
):
    real_sleep = asyncio.sleep
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    history, attempts = [], []
    storage = MemoryManager()
    sid = chat.session_id("alice")

    async def pause():
        entered.set()
        try:
            await release.wait()
        finally:
            finished.set()

    class Redis(StoredRedis):
        async def set(self, key, value, **kwargs):
            last = json.loads(value)["turns"][-1:]
            if mode.startswith("redis") and last and last[0]["role"] == "assistant":
                if mode == "redis-uncertain-timeout":
                    await super().set(key, value, **kwargs)
                await pause()
                if mode == "redis-failure":
                    return False
            return await super().set(key, value, **kwargs)

    async def store(conversation_id, role, content, *args, **kwargs):
        attempts.append((role, content))
        if role == "assistant" and mode.startswith("db"):
            if mode == "db-uncertain-timeout":
                history.append((role, content))
            await pause()
            if mode == "db-false":
                return False
        history.append((role, content))
        return True

    storage._redis = Redis()
    await storage.save_session(sid, SessionState(conversation_id="c"))
    with _patched_pipeline(
        provider="offline-test",
        needs_agent=route == "agent",
        stream_tokens=["Complete answer."],
        agent_response="Complete answer.",
    ) as pipeline:
        pipeline.memory.add_turn.side_effect = storage.add_turn
        pipeline.memory.save_session.side_effect = storage.save_session
        pipeline.store_message.side_effect = store
        monkeypatch.setattr(chat_handlers, "_TURN_FINALIZATION_TIMEOUT_SECONDS", 0.03)
        monkeypatch.setattr(
            chat_handlers.ChatPromptBuilder, "build", AsyncMock(return_value="system")
        )
        monkeypatch.setattr(chat, "memory", storage)
        monkeypatch.setattr(chat, "ws", pipeline.ws)
        monkeypatch.setattr(chat, "_handle_message", chat_handlers._handle_message)
        task = asyncio.create_task(
            chat._admit_turn("message", "Current question", "alice")
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel("original cancellation")
            await real_sleep(0)
            task.cancel("repeated cancellation")
            await real_sleep(0)
            assert sid in storage._session_locks
            if "timeout" not in mode:
                release.set()
            done, _ = await asyncio.wait({task}, timeout=0.5)
            assert task in done, "interrupted storage ignored its cleanup budget"
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            assert error.value.args == ("original cancellation",)
            assert finished.is_set()
            assert attempts == [
                ("user", "Current question"),
                ("assistant", "Complete answer."),
            ]
            expected_history = [("user", "Current question")]
            if mode.startswith("redis") or mode == "db-uncertain-timeout":
                expected_history.append(("assistant", "Complete answer."))
            assert history == expected_history, (
                "no retry or durable deletion is allowed"
            )
            state = await storage.get_session(sid)
            expected_live = [{"role": "user", "content": "Current question"}]
            if mode in ("db-false", "redis-uncertain-timeout"):
                expected_live.append(
                    {"role": "assistant", "content": "Complete answer."}
                )
            assert state.turns == expected_live
            assert not storage._session_locks
            assert not any(
                pending.get_name() == "completed-chat-reply-finalization"
                for pending in asyncio.all_tasks()
                if not pending.done()
            )
            pipeline.ws.send_done.assert_awaited_once()
            pipeline.background_extraction.assert_not_called()
            pipeline.background_compaction.assert_not_called()
            pipeline.background_image_gen.assert_not_called()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("route", ["direct", "agent"])
async def test_healthy_finalization_has_no_new_cleanup_deadline(monkeypatch, route):
    real_sleep = asyncio.sleep
    entered, release = asyncio.Event(), asyncio.Event()

    async def store(conversation_id, role, content, *args, **kwargs):
        if role == "assistant":
            entered.set()
            await release.wait()
        return True

    with _patched_pipeline(
        provider="offline-test", needs_agent=route == "agent"
    ) as pipeline:
        pipeline.store_message.side_effect = store
        monkeypatch.setattr(chat_handlers, "_TURN_FINALIZATION_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(
            chat_handlers.ChatPromptBuilder, "build", AsyncMock(return_value="system")
        )
        task = asyncio.create_task(
            chat_handlers._handle_message(
                "hello", SessionState(conversation_id="c"), "alice"
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await real_sleep(0.03)
            assert not task.done(), (
                "cleanup timeout incorrectly truncated healthy storage"
            )
            release.set()
            assert await asyncio.wait_for(task, 1)
            pipeline.store_message.assert_awaited()
            assert pipeline.store_message.await_count == 2
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "failure", ["empty-direct", "sentinel-direct", "partial-direct", "sentinel-agent"]
)
async def test_failure_reply_never_calls_success_boundary(monkeypatch, failure):
    from app.llm_router import FAILURE_SENTINEL, LLMStreamFailed
    from app.chat_reply import ChatReplyRuntime
    from unittest.mock import Mock

    capture = Mock()
    original_runtime = ChatReplyRuntime

    def runtime(*args, **kwargs):
        original_capture = kwargs["on_completed"]

        def completed(reply):
            capture(reply)
            original_capture(reply)

        kwargs["on_completed"] = completed
        return original_runtime(*args, **kwargs)

    async def stream(*args):
        yield "Already visible."
        raise LLMStreamFailed("failed stream")

    with _patched_pipeline(
        provider="offline-test",
        needs_agent=failure == "sentinel-agent",
        stream_tokens=[] if failure == "empty-direct" else [FAILURE_SENTINEL],
        agent_response=FAILURE_SENTINEL,
    ) as pipeline:
        monkeypatch.setattr(chat_handlers, "ChatReplyRuntime", runtime)
        monkeypatch.setattr(
            chat_handlers.ChatPromptBuilder, "build", AsyncMock(return_value="system")
        )
        if failure == "partial-direct":
            pipeline.router.stream = stream
        assert (
            await chat_handlers._handle_message(
                "hello", SessionState(conversation_id="c"), "alice"
            )
            is None
        )
        capture.assert_not_called()
        pipeline.store_message.assert_awaited_once()
        assert pipeline.store_message.await_args.args[1] == "user"
        pipeline.background_extraction.assert_not_called()


@pytest.mark.parametrize("route", ["direct", "agent"])
@pytest.mark.parametrize(
    "cancel_cleanup", [False, True], ids=["ordinary-error", "new-cancellation"]
)
@pytest.mark.parametrize("cleanup_result", ["success", "failure", "timeout"])
async def test_cancellation_during_terminal_error_recovery_takes_precedence(
    monkeypatch,
    route,
    cancel_cleanup,
    cleanup_result,
):
    """Fault injection: real WSManager normally absorbs socket-send errors."""
    real_sleep = asyncio.sleep
    real_wait_for = asyncio.wait_for
    deadlines = []
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    storage = MemoryManager()
    storage._redis = StoredRedis()
    sid = chat.session_id("alice")
    await storage.save_session(sid, SessionState(conversation_id="c"))
    history = []

    async def wait_for(awaitable, timeout):
        if (
            isinstance(awaitable, asyncio.Task)
            and awaitable.get_name() == "completed-chat-reply-finalization"
        ):
            deadlines.append(timeout)
        return await real_wait_for(awaitable, timeout)

    async def store(conversation_id, role, content, *args, **kwargs):
        if role == "assistant":
            entered.set()
            try:
                await release.wait()
            finally:
                finished.set()
            if cleanup_result == "failure":
                raise RuntimeError("injected storage failure")
        history.append((role, content))
        return True

    with _patched_pipeline(
        provider="offline-test",
        needs_agent=route == "agent",
        stream_tokens=["Complete answer."],
        agent_response="Complete answer.",
    ) as pipeline:
        pipeline.memory.add_turn.side_effect = storage.add_turn
        pipeline.memory.save_session.side_effect = storage.save_session
        pipeline.store_message.side_effect = store
        pipeline.ws.send_done.side_effect = RuntimeError("injected terminal error")
        monkeypatch.setattr(chat_handlers, "_TURN_FINALIZATION_TIMEOUT_SECONDS", 0.03)
        monkeypatch.setattr(asyncio, "wait_for", wait_for)
        monkeypatch.setattr(
            chat_handlers.ChatPromptBuilder, "build", AsyncMock(return_value="system")
        )
        monkeypatch.setattr(chat, "memory", storage)
        monkeypatch.setattr(chat, "ws", pipeline.ws)
        monkeypatch.setattr(chat, "_handle_message", chat_handlers._handle_message)
        task = asyncio.create_task(
            chat._admit_turn("message", "Current question", "alice")
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if cancel_cleanup:
                task.cancel("shutdown during terminal-error recovery")
                await real_sleep(0)
                task.cancel("repeated cancellation")
                await real_sleep(0)
            assert not task.done()
            assert sid in storage._session_locks
            assert not finished.is_set()
            if cleanup_result != "timeout":
                release.set()
            done, _ = await asyncio.wait({task}, timeout=0.5)
            assert task in done, "terminal-error cleanup ignored its original deadline"
            if cancel_cleanup:
                with pytest.raises(asyncio.CancelledError) as error:
                    await task
                assert error.value.args == ("shutdown during terminal-error recovery",)
                assert task.cancelled()
                pipeline.ws.send.assert_not_called()
            else:
                await task
                assert not task.cancelled()
                assert [
                    call.args[1]["type"] for call in pipeline.ws.send.await_args_list
                ] == ["error"]
            assert finished.is_set()
            expected = [("user", "Current question")]
            if cleanup_result == "success":
                expected.append(("assistant", "Complete answer."))
            assert history == expected
            assert deadlines == [0.03], "cancellation restarted the cleanup deadline"
            assert (await storage.get_session(sid)).turns == [
                {"role": role, "content": content} for role, content in history
            ]
            assert pipeline.store_message.await_count == 2
            assert not storage._session_locks
            pipeline.ws.send_done.assert_awaited_once()
            pipeline.background_extraction.assert_not_called()
            pipeline.background_compaction.assert_not_called()
            pipeline.background_image_gen.assert_not_called()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
