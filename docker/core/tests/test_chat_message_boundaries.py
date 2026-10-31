"""Offline message-boundary regressions with real session serialization."""

import asyncio
import sys

import pytest
from fastapi import WebSocketDisconnect

from app import chat
from app.chat_handlers import _handle_message
from app.memory import MemoryManager
from app.models import SessionState
from tests.test_chat_handlers_coverage import _patched_pipeline
from tests.test_conversation_review_fixes import Device, endpoint_setup  # noqa: F401
from tests.test_session_authority import StoredRedis


@pytest.mark.parametrize("raw", [
    '[' * 10000 + '0' + ']' * 10000,
    '{"type":"typing","extra":' + '9' * (sys.get_int_max_str_digits() + 1) + '}',
], ids=["excessive-nesting", "oversized-integer"])
async def test_decoder_limit_frame_does_not_disconnect_valid_next_message(
    endpoint_setup, monkeypatch, raw  # noqa: F811 - imported pytest fixture
):
    endpoint, manager, _ = endpoint_setup
    device = Device()
    frames = iter([raw, '{"type":"message","content":"still here"}'])

    async def receive_text():
        try:
            return next(frames)
        except StopIteration:
            raise WebSocketDisconnect() from None

    device.receive_text = receive_text
    handled = []

    async def handle(content, session, user_id):
        handled.append(content)
        await manager.send_done(user_id, 'reply', 'offline-test')

    monkeypatch.setattr(chat, '_handle_message', handle)
    await endpoint(device)
    assert handled == ['still here']
    assert [event['type'] for event in device.output] == ['connected', 'thinking', 'done']
    assert not manager.is_connected('alice')


@pytest.mark.parametrize('failure', ['cancelled', 'unexpected-error'])
@pytest.mark.parametrize('stage', ['prompt', 'direct', 'agent'])
@pytest.mark.parametrize('prior_pairs', [1, 8], ids=['short-context', 'full-context'])
async def test_interrupted_reply_preserves_history_but_rolls_back_live_user_turn(
    monkeypatch, failure, stage, prior_pairs
):
    manager = MemoryManager()
    manager._redis = StoredRedis()
    sid = chat.session_id('alice')
    previous = [
        {'role': role, 'content': f'previous {role} {index}'}
        for index in range(prior_pairs) for role in ('user', 'assistant')
    ]
    await manager.save_session(sid, SessionState(
        conversation_id='c', turns=previous, turn_count=len(previous),
    ))
    entered = asyncio.Event()
    release = asyncio.Event()
    history = []

    async def store(conversation_id, role, content, *args, **kwargs):
        history.append((role, content))
        return True

    async def interrupt(*args, **kwargs):
        entered.set()
        await release.wait()
        if failure == 'unexpected-error':
            raise RuntimeError('unexpected stream parser failure')

    async def stream(*args):
        await interrupt()
        yield 'never delivered'

    with _patched_pipeline(provider='offline-test', needs_agent=stage == 'agent') as pipeline:
        pipeline.memory.add_turn.side_effect = manager.add_turn
        pipeline.memory.save_session.side_effect = manager.save_session
        pipeline.store_message.side_effect = store
        pipeline.router.stream = stream
        if stage == 'prompt':
            pipeline.affection.get_state.side_effect = interrupt
        elif stage == 'agent':
            pipeline.agent.run.side_effect = interrupt
        monkeypatch.setattr(chat, 'memory', manager)
        monkeypatch.setattr(chat, 'ws', pipeline.ws)
        monkeypatch.setattr(chat, '_handle_message', _handle_message)
        task = asyncio.create_task(chat._admit_turn('message', 'interrupted question', 'alice'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if failure == 'cancelled':
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                await task
            restored = await manager.get_session(sid)
            assert restored.turns == previous, 'unanswered turn will contaminate the next prompt'
            assert restored.turn_count == len(previous)
            assert restored.revision == 1, 'failed turn must still invalidate stale extraction'
            assert history == [('user', 'interrupted question')]
            assert not manager._session_locks
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('cleanup_failure', ['repeat-cancel', 'write-failure', 'timeout'])
async def test_cancelled_reply_joins_rollback_without_swallowing_cancellation(
    monkeypatch, cleanup_failure
):
    import json
    from app import chat_handlers

    monkeypatch.setattr(
        chat_handlers, '_TURN_ROLLBACK_TIMEOUT_SECONDS',
        0.01 if cleanup_failure == 'timeout' else 1,
    )
    manager = MemoryManager()
    sid = chat.session_id('alice')
    rollback_started = asyncio.Event()
    rollback_release = asyncio.Event()
    rollback_finished = asyncio.Event()

    class RollbackRedis(StoredRedis):
        interrupt_cleanup = False

        async def set(self, key, value, **kwargs):
            if self.interrupt_cleanup and not json.loads(value)['turns']:
                rollback_started.set()
                try:
                    await rollback_release.wait()
                    if cleanup_failure == 'write-failure':
                        return False
                finally:
                    rollback_finished.set()
            return await super().set(key, value, **kwargs)

    manager._redis = RollbackRedis()
    await manager.save_session(sid, SessionState(conversation_id='c'))
    entered = asyncio.Event()
    real_sleep = asyncio.sleep

    async def stream(*args):
        entered.set()
        await asyncio.Event().wait()
        yield 'never delivered'

    with _patched_pipeline(provider='offline-test') as pipeline:
        pipeline.memory.add_turn.side_effect = manager.add_turn
        pipeline.memory.save_session.side_effect = manager.save_session
        pipeline.router.stream = stream
        monkeypatch.setattr(chat, 'memory', manager)
        monkeypatch.setattr(chat, 'ws', pipeline.ws)
        monkeypatch.setattr(chat, '_handle_message', _handle_message)
        task = asyncio.create_task(chat._admit_turn('message', 'interrupted question', 'alice'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            manager._redis.interrupt_cleanup = True
            task.cancel()
            await asyncio.wait_for(rollback_started.wait(), 1)
            if cleanup_failure in ('repeat-cancel', 'timeout'):
                task.cancel()
                await real_sleep(0)
                assert sid in manager._session_locks, 'cleanup released ownership before its write settled'
            if cleanup_failure == 'timeout':
                await asyncio.wait({task}, timeout=0.2)
                assert task.done(), 'stalled Redis rollback ignored cancellation without a deadline'
                assert rollback_finished.is_set(), 'timed-out rollback task was left running'
            else:
                rollback_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            if cleanup_failure == 'repeat-cancel':
                restored = await manager.get_session(sid)
                assert restored.turns == []
                assert restored.turn_count == 0
            assert not manager._session_locks
        finally:
            rollback_release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('failure', ['empty', 'sentinel', 'partial', 'agent'])
async def test_failed_reply_restores_full_context_window(monkeypatch, failure):
    from app.llm_router import FAILURE_SENTINEL, LLMStreamFailed

    manager = MemoryManager()
    manager._redis = StoredRedis()
    sid = chat.session_id('alice')
    previous = [
        {'role': role, 'content': f'previous {role} {index}'}
        for index in range(8) for role in ('user', 'assistant')
    ]
    await manager.save_session(sid, SessionState(
        conversation_id='c', turns=previous, turn_count=16,
    ))

    async def partial(*args):
        yield 'Unfinished answer.'
        raise LLMStreamFailed('interrupted')

    with _patched_pipeline(
        provider='offline-test', stream_tokens=[] if failure == 'empty' else [FAILURE_SENTINEL],
        needs_agent=failure == 'agent', agent_response=FAILURE_SENTINEL,
    ) as pipeline:
        pipeline.memory.add_turn.side_effect = manager.add_turn
        pipeline.memory.save_session.side_effect = manager.save_session
        if failure == 'partial':
            pipeline.router.stream = partial
        monkeypatch.setattr(chat, 'memory', manager)
        monkeypatch.setattr(chat, 'ws', pipeline.ws)
        monkeypatch.setattr(chat, '_handle_message', _handle_message)
        await chat._admit_turn('message', 'unanswered question', 'alice')
        restored = await manager.get_session(sid)
        assert restored.turns == previous, 'rollback lost the oldest context turn'
        assert restored.turn_count == 16
        assert restored.revision == 1


async def test_context_rollback_preserves_current_metadata(monkeypatch):
    from app import chat_handlers

    manager = MemoryManager()
    manager._redis = StoredRedis()
    sid = chat.session_id('alice')
    previous = SessionState(
        conversation_id='c', revision=3, mood='composed', turn_count=80,
        turns=[
            {'role': role, 'content': f'previous {role} {index}'}
            for index in range(8) for role in ('user', 'assistant')
        ],
    )
    current = previous.model_copy(deep=True)
    current.revision = 4
    current.mood = 'tender'
    current.mission_description = 'current mission'
    current.context_summary = 'current summary'
    await manager.add_turn(sid, 'user', 'unanswered', current)
    monkeypatch.setattr(chat_handlers, 'memory', manager)
    async with manager.session_owner(sid):
        await chat_handlers._drop_unanswered_turn(sid, current, previous=previous)
    restored = await manager.get_session(sid)
    assert restored.turns == previous.turns
    assert restored.turn_count == 80
    assert restored.revision == 4
    assert restored.mood == 'tender'
    assert restored.mission_description == 'current mission'
    assert restored.context_summary == 'current summary'
