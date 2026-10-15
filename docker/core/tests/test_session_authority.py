"""Deterministic races at the chat/storage boundary; no live providers."""
import asyncio
from unittest.mock import AsyncMock

import pytest
import redis

from app.memory import MemoryManager
from app.models import SessionState
from app import chat


class StoredRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.values[key] = value
        return True


@pytest.fixture
def manager(monkeypatch):
    manager = MemoryManager()
    manager._redis = StoredRedis()
    monkeypatch.setattr(chat, 'memory', manager)
    return manager


@pytest.mark.asyncio
async def test_two_open_devices_read_latest_and_serialize_whole_exchange(manager, monkeypatch):
    sid = chat.session_id('alice')
    await manager.save_session(sid, SessionState(conversation_id='c'))
    entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events = []

    async def handle(content, state, user):
        events.append((content, 'start', [t['content'] for t in state.turns]))
        await manager.add_turn(chat.session_id(user), 'user', content, state)
        if content == 'first':
            entered.set()
            await release.wait()
        await manager.add_turn(chat.session_id(user), 'assistant', content + '-reply', state)
        events.append((content, 'done'))

    monkeypatch.setattr(chat, '_handle_message', handle)
    first = asyncio.create_task(chat._admit_turn('message', 'first', 'alice'))
    await entered.wait()

    async def second_device():
        queued.set()
        await chat._admit_turn('message', 'second', 'alice')

    second = asyncio.create_task(second_device())
    await queued.wait()
    await manager.save_session(chat.session_id('bob'), SessionState(conversation_id='b'))
    await asyncio.wait_for(chat._admit_turn('message', 'other', 'bob'), 1)
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)
    state = await manager.get_session(sid)
    assert [t['content'] for t in state.turns] == ['first', 'first-reply', 'second', 'second-reply']
    assert events[-2] == ('second', 'start', ['first', 'first-reply'])
    assert state.revision == 2
    assert not manager._session_locks


@pytest.mark.asyncio
async def test_failed_turn_still_invalidates_delayed_state(manager, monkeypatch):
    sid = chat.session_id('alice')
    await manager.save_session(sid, SessionState(conversation_id='c'))
    before = await manager.get_session(sid)
    from app import chat_handlers
    monkeypatch.setattr(chat_handlers, 'memory', manager)
    async def abort(content, state, user):
        await manager.add_turn(sid, 'user', content, state)
        await chat_handlers._drop_unanswered_turn(sid, state)
        raise RuntimeError('aborted')
    monkeypatch.setattr(chat, '_handle_message', abort)
    outgoing = AsyncMock()
    monkeypatch.setattr(chat, 'ws', outgoing)
    await chat._admit_turn('message', 'new', 'alice')
    assert outgoing.send.await_args.args[1]['type'] == 'error'
    current = await manager.get_session(sid)
    assert current.revision > before.revision
    assert current.turns == before.turns


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['get', 'set'])
async def test_redis_failure_is_not_missing_or_success(manager, monkeypatch, operation):
    class BrokenRedis:
        async def get(self, *args, **kwargs):
            raise redis.ConnectionError('offline')
        async def set(self, *args, **kwargs):
            raise redis.ConnectionError('offline')
    manager._redis = BrokenRedis()
    monkeypatch.setattr('app.memory.redis.from_url', lambda *a, **kw: BrokenRedis())
    with pytest.raises(Exception, match='offline'):
        if operation == 'get':
            await manager.get_session('alice')
        else:
            await manager.save_session('alice', SessionState(conversation_id='c'))


@pytest.mark.asyncio
async def test_extracted_state_guard_blocks_all_stale_mood_and_mission_effects(manager, monkeypatch):
    from app import background
    from unittest.mock import MagicMock
    sid = chat.session_id('alice')
    snapshot = SessionState(conversation_id='c', revision=1)
    latest = snapshot.model_copy(deep=True)
    latest.revision = 2
    latest.mood = 'happy'
    await manager.save_session(sid, latest)
    monkeypatch.setattr(background, 'memory', manager)
    outgoing = AsyncMock()
    proactive = MagicMock()
    monkeypatch.setattr(background, 'ws', outgoing)
    monkeypatch.setattr(background, 'proactive', proactive)
    persist = MagicMock(side_effect=AssertionError('stale persistent mood write'))
    monkeypatch.setattr(background, 'get_conn_autocommit', persist)
    assert not await background._apply_extracted_state(snapshot, 'battle_ready', 'alice')
    current = await manager.get_session(sid)
    assert current.mood == 'happy'
    assert current.mission_description is None
    assert not outgoing.send_mood.called
    assert not proactive.start_mission.called
    assert not proactive.set_last_mood.called


@pytest.mark.asyncio
async def test_compaction_merges_latest_turns_and_metadata(manager, monkeypatch):
    from app import background, fact_extractor
    sid = chat.session_id('alice')
    original = SessionState(conversation_id='c', context_summary='old', turns=[
        {'role': 'user', 'content': str(n)} for n in range(6)
    ])
    await manager.save_session(sid, original)
    monkeypatch.setattr(background, 'memory', manager)
    monkeypatch.setattr(background.context, 'COMPACT_THRESHOLD', 6)
    monkeypatch.setattr(background.context, 'COMPACT_KEEP_RAW', 2)
    entered, release = asyncio.Event(), asyncio.Event()
    async def compact(turns):
        entered.set()
        await release.wait()
        return 'summary'
    monkeypatch.setattr(fact_extractor, 'compact_turns', compact)
    task = asyncio.create_task(background.background_compaction(original, 'alice'))
    await entered.wait()
    latest = await manager.get_session(sid)
    latest.mood = 'happy'
    latest.mission_description = 'new mission'
    latest.turns.append({'role': 'assistant', 'content': 'new'})
    await manager.save_session(sid, latest)
    release.set()
    await task
    current = await manager.get_session(sid)
    assert [t['content'] for t in current.turns] == ['4', '5', 'new']
    assert current.context_summary == 'summary'
    assert current.mood == 'happy'
    assert current.mission_description == 'new mission'
    assert original.context_summary == 'old'


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['conversation_id', 'context_summary', 'turns'])
async def test_compaction_rejects_changed_identity_summary_or_prefix(manager, monkeypatch, changed):
    from app import background, fact_extractor
    sid = chat.session_id('alice')
    original = SessionState(conversation_id='c', turns=[{'role': 'user', 'content': str(n)} for n in range(4)])
    await manager.save_session(sid, original)
    monkeypatch.setattr(background, 'memory', manager)
    monkeypatch.setattr(background.context, 'COMPACT_THRESHOLD', 4)
    monkeypatch.setattr(background.context, 'COMPACT_KEEP_RAW', 2)
    async def compact(turns):
        latest = await manager.get_session(sid)
        if changed == 'turns':
            latest.turns[0]['content'] = 'changed'
        else:
            setattr(latest, changed, 'changed')
        await manager.save_session(sid, latest)
        return 'stale summary'
    monkeypatch.setattr(fact_extractor, 'compact_turns', compact)
    await background.background_compaction(original, 'alice')
    current = await manager.get_session(sid)
    assert current.context_summary != 'stale summary'
    assert len(current.turns) == 4


@pytest.mark.asyncio
async def test_endpoint_two_connected_devices_use_latest_session(manager, monkeypatch):
    """Exercise endpoint wiring, not only the admission helper."""
    import json
    from fastapi import FastAPI, WebSocketDisconnect
    from starlette.websockets import WebSocketState
    from unittest.mock import MagicMock
    from app.ws_manager import WSManager

    class Device:
        query_params = {'token': 'test'}
        application_state = client_state = WebSocketState.CONNECTED
        def __init__(self):
            self.frames = asyncio.Queue()
            self.receiving = asyncio.Event()
            self.output = []
        async def accept(self):
            pass
        async def send_text(self, data):
            self.output.append(json.loads(data))
        async def receive_text(self):
            self.receiving.set()
            frame = await self.frames.get()
            if frame is None:
                raise WebSocketDisconnect()
            return json.dumps(frame)

    ws = WSManager()
    monkeypatch.setattr(chat, 'ws', ws)
    monkeypatch.setattr(chat, 'proactive', MagicMock(mission_active=False))
    monkeypatch.setattr('app.auth.get_user_from_token', AsyncMock(return_value='alice'))
    monkeypatch.setattr('app.warmup.warm_in_background', lambda: None)
    for name in ['_maybe_reflect_on_return', '_maybe_oath_on_connect']:
        monkeypatch.setattr(chat, name, AsyncMock())
    monkeypatch.setattr('app.rituals.on_connect', AsyncMock())
    monkeypatch.setattr('app.wardrobe.warm_today', AsyncMock())
    await manager.save_session(chat.session_id('alice'), SessionState(conversation_id='c'))
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def handle(content, state, user):
        await manager.add_turn(chat.session_id(user), 'user', content, state)
        await ws.send_token(user, content)
        if content == 'first':
            started.set()
            await release.wait()
        await manager.add_turn(chat.session_id(user), 'assistant', content + '-reply', state)
        await ws.send_done(user, content, 'test')
        if content == 'second':
            finished.set()
    monkeypatch.setattr(chat, '_handle_message', handle)
    app = FastAPI()
    chat.register_websocket(app)
    endpoint = next(route.endpoint for route in app.routes if route.path == '/ws')
    one, two = Device(), Device()
    endpoints = [asyncio.create_task(endpoint(device)) for device in [one, two]]
    try:
        await asyncio.wait_for(asyncio.gather(one.receiving.wait(), two.receiving.wait()), 1)
        await one.frames.put({'type': 'message', 'content': 'first'})
        await started.wait()
        await two.frames.put({'type': 'message', 'content': 'second'})
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        for device in [one, two]:
            response_events = [event['type'] for event in device.output if event['type'] in ['token', 'done']]
            assert response_events == ['token', 'done', 'token', 'done']
        state = await manager.get_session(chat.session_id('alice'))
        assert [turn['content'] for turn in state.turns] == ['first', 'first-reply', 'second', 'second-reply']
    finally:
        release.set()
        for device in [one, two]:
            await device.frames.put(None)
        await asyncio.gather(*endpoints)
    assert not ws.connected


@pytest.mark.asyncio
async def test_concurrent_initialization_creates_one_session_and_preserves_live_mood(manager, monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock
    entered, release = asyncio.Event(), asyncio.Event()
    created = []
    async def create(conversation_id, user_id, *, strict):
        created.append(conversation_id)
        entered.set()
        await release.wait()
    @asynccontextmanager
    async def connection():
        yield MagicMock(execute=AsyncMock(return_value=MagicMock(fetchone=AsyncMock(return_value=('tender',)))))
    monkeypatch.setattr(chat, '_create_conversation', create)
    monkeypatch.setattr(chat, 'get_conn', connection)
    monkeypatch.setattr(chat, 'ws', AsyncMock())
    monkeypatch.setattr(chat, 'proactive', MagicMock(mission_active=False))
    first = asyncio.create_task(chat._initialize_session('alice'))
    await entered.wait()
    second = asyncio.create_task(chat._initialize_session('alice'))
    release.set()
    one, two = await asyncio.gather(first, second)
    assert one.conversation_id == two.conversation_id
    assert len(created) == 1
    one.mood = 'happy'
    await manager.save_session(chat.session_id('alice'), one)
    third = await chat._initialize_session('alice')
    assert third.mood == 'happy'


@pytest.mark.asyncio
async def test_voice_owns_until_audio_finishes(manager, monkeypatch):
    sid = chat.session_id('alice')
    await manager.save_session(sid, SessionState(conversation_id='c'))
    entered, release = asyncio.Event(), asyncio.Event()
    order = []
    async def voice(audio, state, user):
        order.append('voice-text')
        entered.set()
        await release.wait()
        order.append('voice-audio')
    async def text(content, state, user):
        order.append('next-text')
    monkeypatch.setattr(chat, '_handle_voice', voice)
    monkeypatch.setattr(chat, '_handle_message', text)
    first = asyncio.create_task(chat._admit_turn('voice_end', 'audio', 'alice'))
    await entered.wait()
    second = asyncio.create_task(chat._admit_turn('message', 'next', 'alice'))
    release.set()
    await asyncio.gather(first, second)
    assert order == ['voice-text', 'voice-audio', 'next-text']


@pytest.mark.asyncio
async def test_initialization_does_not_publish_session_without_history_parent(manager, monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock
    from app import helpers
    @asynccontextmanager
    async def unavailable():
        raise RuntimeError('history unavailable')
        yield
    monkeypatch.setattr(chat, 'get_conn', unavailable)
    monkeypatch.setattr('app.db.get_conn_autocommit', unavailable)
    monkeypatch.setattr(chat, '_create_conversation', helpers.create_conversation)
    monkeypatch.setattr(chat, 'ws', AsyncMock())
    monkeypatch.setattr(chat, 'proactive', MagicMock(mission_active=False))
    with pytest.raises(RuntimeError, match='history unavailable'):
        await chat._initialize_session('alice')
    assert await manager.get_session(chat.session_id('alice')) is None


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_split_session_ownership(manager):
    holding, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []
    async def holder():
        async with manager.session_owner('u'):
            order.append('holder')
            holding.set()
            await release.wait()
            order.append('released')
    async def waiter():
        queued.set()
        async with manager.session_owner('u'):
            order.append('waiter')
    first = asyncio.create_task(holder())
    await holding.wait()
    cancelled = asyncio.create_task(waiter())
    await queued.wait()
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    queued.clear()
    last = asyncio.create_task(waiter())
    await queued.wait()
    assert order == ['holder']
    release.set()
    await asyncio.gather(first, last)
    assert order == ['holder', 'released', 'waiter']
    assert not manager._session_locks
