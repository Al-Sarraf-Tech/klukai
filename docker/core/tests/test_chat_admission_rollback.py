"""Offline cancellation probes before prompt construction starts."""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from app import chat
from app.conversation_store import store_message
from app.memory import MemoryManager
from app.models import SessionState
from tests.test_chat_handlers_coverage import _patched_pipeline
from tests.test_session_authority import StoredRedis


@pytest.mark.parametrize(
    "stage", ["redis-written", "db-before-commit", "db-after-commit", "warning"]
)
@pytest.mark.parametrize("prior_pairs", [0, 8], ids=["empty-window", "full-window"])
async def test_cancelled_user_persistence_restores_prior_live_window(
    stage, prior_pairs
):
    entered = asyncio.Event()
    release = asyncio.Event()
    prior_history = [
        ("user", "earlier durable question"),
        ("assistant", "earlier durable answer"),
    ]
    history = list(prior_history)
    statements = []
    previous = [
        {"role": role, "content": f"previous {role} {index}"}
        for index in range(prior_pairs)
        for role in ("user", "assistant")
    ]
    sid = chat.session_id("alice")

    class AdmissionRedis(StoredRedis):
        async def set(self, key, value, **kwargs):
            result = await super().set(key, value, **kwargs)
            turns = json.loads(value)["turns"]
            if stage == "redis-written" and turns and turns[-1]["content"] == "first":
                entered.set()
                await release.wait()
            return result

    manager = MemoryManager()
    manager._redis = AdmissionRedis()
    await manager.save_session(
        sid,
        SessionState(
            conversation_id="c",
            turns=previous,
            turn_count=len(previous),
            revision=7,
            mood="tender",
            mission_description="active mission",
            mission_interval=17,
            mission_started_at="2026-10-22T07:00:00",
            context_summary="earlier context",
        ),
    )

    class Transaction:
        def __init__(self):
            self.pending = []

        async def execute(self, sql, params):
            statements.append(sql)
            if sql.startswith("INSERT INTO companion_messages"):
                self.pending.append((params[1], params[2]))

        async def commit(self):
            if stage == "warning":
                raise RuntimeError("offline fake durable store failure")
            if stage == "db-before-commit":
                entered.set()
                await release.wait()
            history.extend(self.pending)
            if stage == "db-after-commit":
                entered.set()
                await release.wait()

    @asynccontextmanager
    async def connection():
        yield Transaction()

    async def send(user_id, event):
        if event["type"] == "warning":
            entered.set()
            await release.wait()

    with (
        _patched_pipeline(provider="offline-test") as pipeline,
        patch.object(chat, "memory", manager),
        patch.object(chat, "ws", pipeline.ws),
        patch("app.db.get_conn", connection),
    ):
        pipeline.memory.add_turn.side_effect = manager.add_turn
        pipeline.memory.save_session.side_effect = manager.save_session
        pipeline.store_message.side_effect = store_message
        pipeline.ws.send.side_effect = send
        task = asyncio.create_task(chat._admit_turn("message", "first", "alice"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            restored = await manager.get_session(sid)
            assert restored.turns == previous, (
                "unanswered user survives into the next prompt"
            )
            assert restored.turn_count == len(previous)
            assert restored.revision == 8
            assert restored.mood == "tender"
            assert restored.mission_description == "active mission"
            assert restored.mission_interval == 17
            assert restored.mission_started_at == "2026-10-22T07:00:00"
            assert restored.context_summary == "earlier context"
            assert history == prior_history + (
                [("user", "first")] if stage == "db-after-commit" else []
            )
            assert all(
                sql.startswith(
                    ("INSERT INTO companion_messages", "UPDATE companion_conversations")
                )
                for sql in statements
            )
            pipeline.router.needs_agent.assert_not_called()
            pipeline.ws.send_done.assert_not_called()
            assert not manager._session_locks
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_unacknowledged_user_write_restores_live_window(monkeypatch):
    import redis

    previous = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "reply"},
    ]
    manager = MemoryManager()
    sid = chat.session_id("alice")
    durable = {}

    class LostReplyRedis(StoredRedis):
        def __init__(self, retry=False):
            self.values = durable
            self.retry = retry

        async def set(self, key, value, **kwargs):
            turns = json.loads(value)["turns"]
            if turns and turns[-1]["content"] == "first":
                if not self.retry:
                    await super().set(key, value, **kwargs)
                raise redis.ConnectionError(
                    "offline simulated lost write acknowledgement"
                )
            return await super().set(key, value, **kwargs)

        async def aclose(self):
            pass

    manager._redis = LostReplyRedis()
    replacement = LostReplyRedis(retry=True)
    monkeypatch.setattr(
        "app.memory_session.redis.from_url", lambda *a, **kw: replacement
    )
    await manager.save_session(
        sid, SessionState(conversation_id="c", turns=previous, turn_count=2)
    )
    with (
        _patched_pipeline(provider="offline-test") as pipeline,
        patch.object(chat, "memory", manager),
        patch.object(chat, "ws", pipeline.ws),
    ):
        pipeline.memory.add_turn.side_effect = manager.add_turn
        pipeline.memory.save_session.side_effect = manager.save_session
        await chat._admit_turn("message", "first", "alice")
        restored = await manager.get_session(sid)
        assert restored.turns == previous, (
            "failed admission left an orphan despite restored Redis access"
        )
        assert restored.turn_count == 2
        assert restored.revision == 1
        assert pipeline.ws.send.await_args.args[1]["type"] == "error"
        pipeline.store_message.assert_not_called()
        pipeline.router.needs_agent.assert_not_called()
        assert not manager._session_locks


async def test_repeated_admission_cancel_settles_before_second_device_turn():
    from app import chat_handlers
    from app.ws_manager import WSManager
    from tests.test_conversation_review_fixes import Device

    previous = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "reply"},
    ]
    manager = MemoryManager()
    sid = chat.session_id("alice")
    persisted = asyncio.Event()
    rollback_entered = asyncio.Event()
    rollback_release = asyncio.Event()
    first_release = asyncio.Event()
    queued = asyncio.Event()
    history = []
    real_sleep = asyncio.sleep

    class RollbackRedis(StoredRedis):
        async def set(self, key, value, **kwargs):
            if persisted.is_set() and json.loads(value)["turns"] == previous:
                rollback_entered.set()
                await rollback_release.wait()
            return await super().set(key, value, **kwargs)

    manager._redis = RollbackRedis()
    await manager.save_session(
        sid, SessionState(conversation_id="c", turns=previous, turn_count=2)
    )
    sockets = WSManager()
    first_device, second_device = Device(), Device()
    await sockets.connect(first_device, "alice")
    await sockets.connect(second_device, "alice")

    async def store(conversation_id, role, content, *args, **kwargs):
        history.append((role, content))
        if content == "first":
            persisted.set()
            await first_release.wait()
        return True

    async def second_turn():
        queued.set()
        await chat._admit_turn("message", "second", "alice", second_device)

    with (
        _patched_pipeline(
            provider="offline-test", stream_tokens=["Second reply."]
        ) as pipeline,
        patch.object(chat, "memory", manager),
        patch.object(chat, "ws", sockets),
        patch.object(chat_handlers, "ws", sockets),
    ):
        pipeline.memory.add_turn.side_effect = manager.add_turn
        pipeline.memory.save_session.side_effect = manager.save_session
        pipeline.store_message.side_effect = store
        first = asyncio.create_task(
            chat._admit_turn("message", "first", "alice", first_device)
        )
        second = None
        try:
            await asyncio.wait_for(persisted.wait(), 1)
            first.cancel()
            await asyncio.wait_for(rollback_entered.wait(), 1)
            second = asyncio.create_task(second_turn())
            await asyncio.wait_for(queued.wait(), 1)
            first.cancel()
            await real_sleep(0)
            assert sid in manager._session_locks
            assert not first.done(), "repeat cancellation escaped the owned rollback"
            assert not second.done(), "second device overtook the unsettled rollback"
            assert history == [("user", "first")]
            rollback_release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            await asyncio.wait_for(second, 1)
            restored = await manager.get_session(sid)
            assert restored.turns == previous + [
                {"role": "user", "content": "second"},
                {"role": "assistant", "content": "Second reply."},
            ]
            assert restored.turn_count == 4
            assert restored.revision == 2
            assert history == [
                ("user", "first"),
                ("user", "second"),
                ("assistant", "Second reply."),
            ]
            assert any(event["type"] == "done" for event in second_device.output)
            assert not manager._session_locks
        finally:
            first_release.set()
            rollback_release.set()
            tasks = [first] + ([second] if second is not None else [])
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await sockets.close()
