"""Failure-path regressions from the independent conversation review."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis
from fastapi import FastAPI, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app import chat
from app.memory import MemoryManager, SessionUnavailable
from app.models import SessionState
from app.ws_manager import WSManager
from tests.test_chat_handlers_coverage import _patched_pipeline
from tests.test_session_authority import StoredRedis


class Device:
    query_params = {'token': 'test'}
    application_state = client_state = WebSocketState.CONNECTED

    def __init__(self):
        self.frames = asyncio.Queue()
        self.receiving = asyncio.Event()
        self.message_received = asyncio.Event()
        self.output = []
        self.close = AsyncMock()
        self.block_error = None
        self.error_started = None
        self.slow_token = False

    async def accept(self):
        pass

    async def send_text(self, data):
        event = json.loads(data)
        if event['type'] == 'error' and self.block_error:
            self.error_started.set()
            await self.block_error.wait()
        if event['type'] == 'token' and self.slow_token:
            await asyncio.Event().wait()
        self.output.append(event)

    async def receive_text(self):
        self.receiving.set()
        frame = await self.frames.get()
        if frame is None:
            raise WebSocketDisconnect()
        self.message_received.set()
        return json.dumps(frame)


@pytest.fixture
async def endpoint_setup(monkeypatch):
    memory = MemoryManager()
    memory._redis = StoredRedis()
    manager = WSManager()
    await memory.save_session(chat.session_id('alice'), SessionState(conversation_id='c'))
    for target, value in {
        'app.chat.memory': memory,
        'app.chat.ws': manager,
        'app.chat.proactive': MagicMock(mission_active=False),
        'app.auth.get_user_from_token': AsyncMock(return_value='alice'),
        'app.auth.get_user_from_session_hash': AsyncMock(return_value='alice'),
        'app.warmup.warm_in_background': lambda: None,
        'app.chat._maybe_reflect_on_return': AsyncMock(),
        'app.chat._maybe_oath_on_connect': AsyncMock(),
        'app.rituals.on_connect': AsyncMock(),
        'app.wardrobe.warm_today': AsyncMock(),
    }.items():
        monkeypatch.setattr(target, value)
    app = FastAPI()
    chat.register_websocket(app)
    endpoint = next(route.endpoint for route in app.routes if route.path == '/ws')
    return endpoint, manager, memory


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [SessionUnavailable, RuntimeError])
async def test_terminal_error_finishes_before_another_device_reply(endpoint_setup, monkeypatch, failure):
    endpoint, manager, _ = endpoint_setup
    first, second = Device(), Device()
    error_started, release_error = asyncio.Event(), asyncio.Event()
    second_started, release_second = asyncio.Event(), asyncio.Event()
    for device in (first, second):
        device.block_error = release_error
        device.error_started = error_started

    async def handle(content, state, user):
        if content == 'first':
            raise failure('first turn failed')
        second_started.set()
        await manager.send_token(user, 'second')
        await release_second.wait()
        await manager.send_done(user, 'second', 'test')

    monkeypatch.setattr(chat, '_handle_message', handle)
    admission_attempted = asyncio.Event()
    original_admit = chat._admit_turn
    async def admit(kind, content, user, websocket=None):
        if content == 'second':
            admission_attempted.set()
        await original_admit(kind, content, user, websocket)
    monkeypatch.setattr(chat, '_admit_turn', admit)
    tasks = [asyncio.create_task(endpoint(device)) for device in (first, second)]
    try:
        await asyncio.wait_for(asyncio.gather(first.receiving.wait(), second.receiving.wait()), 1)
        await first.frames.put({'type': 'message', 'content': 'first'})
        await asyncio.wait_for(error_started.wait(), 1)
        await second.frames.put({'type': 'message', 'content': 'second'})
        await asyncio.wait_for(admission_attempted.wait(), 1)
        assert not second_started.is_set(), 'a new response escaped ahead of the previous terminal error'
        release_error.set()
        await asyncio.wait_for(second_started.wait(), 1)
        release_second.set()
        for device in (first, second):
            await device.frames.put(None)
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        for device in (first, second):
            assert [event['type'] for event in device.output] == ['connected', 'error', 'token', 'done']
    finally:
        release_error.set()
        release_second.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('close_hangs', [False, True])
async def test_timed_out_socket_closes_and_cannot_process_silent_turns(endpoint_setup, monkeypatch, close_hangs):
    endpoint, manager, _ = endpoint_setup
    monkeypatch.setattr('app.ws_manager.SEND_TIMEOUT_SECONDS', .01)
    # Set only after the production constant exists; the red run still verifies
    # the missing close/endpoint retirement rather than failing at setup.
    monkeypatch.setattr('app.ws_manager.CLOSE_TIMEOUT_SECONDS', .01, raising=False)
    device = Device()
    device.slow_token = True
    if close_hangs:
        async def hang(**kwargs):
            await asyncio.Event().wait()
        device.close.side_effect = hang
    calls = []
    async def handle(content, state, user):
        calls.append(content)
        await manager.send_token(user, content)
        await manager.send_done(user, content, 'test')
    monkeypatch.setattr(chat, '_handle_message', handle)
    await device.frames.put({'type': 'message', 'content': 'first'})
    await device.frames.put({'type': 'message', 'content': 'second'})
    await device.frames.put(None)
    await asyncio.wait_for(endpoint(device), 1)
    assert calls == ['first'], 'removed transport continued accepting messages without outbound delivery'
    device.close.assert_awaited_once()
    assert not manager.is_connected('alice')


@pytest.mark.asyncio
async def test_delivered_assistant_history_survives_redis_finalization_failure(monkeypatch):
    from app.chat_handlers import _handle_message
    state = SessionState(conversation_id='c')
    with _patched_pipeline() as ns:
        class FailingRedis(StoredRedis):
            async def set(self, key, value, **kwargs):
                turns = json.loads(value)['turns']
                if turns and turns[-1]['role'] == 'assistant':
                    raise redis.ConnectionError('assistant write failed')
                return await super().set(key, value, **kwargs)
        manager = MemoryManager()
        manager._redis = FailingRedis()
        ns.memory.add_turn.side_effect = manager.add_turn
        monkeypatch.setattr('app.memory.redis.from_url', lambda *a, **kw: manager._redis)
        with pytest.raises(SessionUnavailable, match='assistant write failed'):
            await _handle_message('hello', state, 'alice')
        assert ns.ws.send_done.await_count == 1
        assert [call.args[1] for call in ns.store_message.await_args_list] == ['user', 'assistant']
        assert ns.store_message.await_args.args[2] == 'Hello, Commander.'


@pytest.mark.asyncio
async def test_device_retired_while_waiting_for_admission_never_runs_handler(endpoint_setup, monkeypatch):
    endpoint, manager, _ = endpoint_setup
    monkeypatch.setattr('app.ws_manager.SEND_TIMEOUT_SECONDS', .01)
    first, second = Device(), Device()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def handle(content, state, user):
        calls.append(content)
        if content == 'first':
            entered.set()
            await release.wait()
        await manager.send_token(user, content)
        await manager.send_done(user, content, 'test')
    monkeypatch.setattr(chat, '_handle_message', handle)
    admission_attempted = asyncio.Event()
    original_admit = chat._admit_turn
    async def admit(kind, content, user, websocket=None):
        if content == 'second':
            admission_attempted.set()
        await original_admit(kind, content, user, websocket)
    monkeypatch.setattr(chat, '_admit_turn', admit)
    tasks = [asyncio.create_task(endpoint(device)) for device in (first, second)]
    try:
        await asyncio.wait_for(asyncio.gather(first.receiving.wait(), second.receiving.wait()), 1)
        second.slow_token = True
        await first.frames.put({'type': 'message', 'content': 'first'})
        await entered.wait()
        await second.frames.put({'type': 'message', 'content': 'second'})
        await asyncio.wait_for(admission_attempted.wait(), 1)
        release.set()
        for device in (first, second):
            await device.frames.put(None)
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert calls == ['first']
        second.close.assert_awaited_once()
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_background_send_timeout_retires_idle_endpoint_even_if_close_hangs(endpoint_setup, monkeypatch):
    endpoint, manager, _ = endpoint_setup
    monkeypatch.setattr('app.ws_manager.SEND_TIMEOUT_SECONDS', .01)
    monkeypatch.setattr('app.ws_manager.CLOSE_TIMEOUT_SECONDS', .01)
    device = Device()
    async def hang(**kwargs):
        await asyncio.Event().wait()
    device.close.side_effect = hang
    task = asyncio.create_task(endpoint(device))
    try:
        await asyncio.wait_for(device.receiving.wait(), 1)
        device.slow_token = True
        await manager.send_token('alice', 'background notification')
        await asyncio.wait_for(asyncio.shield(task), .1)
        assert not manager.is_connected('alice')
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
