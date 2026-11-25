"""The next-turn recall pin survives a restart, and is still read once.

The live reply does not wait on the picker. Redis is a short-lived mirror;
when it is down the in-process pin is the fallback.
"""

import asyncio
import shutil
import subprocess
import time
from unittest.mock import AsyncMock

import pytest

from app import caches, memory_recall
from app.memory import MemoryManager


def episode(text):
    return ("episode-id", text, [], [], 0.5, None)


def _put(store, keys, args):
    gen_key, pin_key = keys
    current = store.data.get(gen_key, "")
    if args[0] < current:
        return 0
    ttl = int(args[2])
    store.data[gen_key] = args[0]
    store.ttls[gen_key] = ttl
    if args[1] == "":
        store.data.pop(pin_key, None)
        store.ttls.pop(pin_key, None)
    else:
        store.data[pin_key] = args[1]
        store.ttls[pin_key] = ttl
    return 1


def _drop(store, keys, args):
    gen_key, pin_key = keys
    current = store.data.get(gen_key, "")
    if current <= args[0]:
        store.data.pop(pin_key, None)
        store.ttls.pop(pin_key, None)
        return 1
    return 0


def _take(store, keys, _args):
    pin_key = keys[0]
    store.ttls.pop(pin_key, None)
    return store.data.pop(pin_key, None)


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.ttls = {}
        self.down = False
        self.hang = False

    async def eval(self, script, numkeys, *rest):
        if self.hang:
            await asyncio.sleep(30)
        if self.down:
            raise ConnectionError("redis down")
        keys = list(rest[:numkeys])
        args = list(rest[numkeys:])
        if script == caches._PIN_PUT:
            return _put(self, keys, args)
        if script == caches._PIN_DROP:
            return _drop(self, keys, args)
        if script == caches._PIN_TAKE:
            return _take(self, keys, args)
        raise AssertionError("unexpected recall-pin script")


@pytest.fixture
def pin_redis(monkeypatch):
    store = FakeRedis()
    monkeypatch.setattr(memory_recall, "_PINNED", {})
    monkeypatch.setattr(memory_recall, "_PENDING_PINS", {})
    monkeypatch.setattr(memory_recall, "_PIN_GEN", {})
    monkeypatch.setattr(memory_recall, "_TOKEN_GEN", {})
    monkeypatch.setattr(caches, "_pin_down_until", 0.0)

    async def client():
        return store

    monkeypatch.setattr(caches, "_pin_client", client)
    return store


def restart():
    memory_recall._PINNED.clear()
    memory_recall._PENDING_PINS.clear()
    memory_recall._PIN_GEN.clear()
    memory_recall._TOKEN_GEN.clear()


async def pin(monkeypatch, query, picked, user="alice"):
    async def fetch(*_args, **_kwargs):
        return [episode(query)] if picked is not None else []

    async def select(*_args, **_kwargs):
        return picked

    monkeypatch.setattr(memory_recall, "fetch_candidates", fetch)
    monkeypatch.setattr(memory_recall, "select", select)
    await MemoryManager().pin_next_recall(query, user)


def test_pin_mirror_stays_short_and_off_the_picker(pin_redis):
    assert 0 < caches.RECALL_PIN_TTL <= 15 * 60
    assert caches._PIN_OP_TIMEOUT_S <= 0.5
    assert pin_redis is not None


def test_take_pinned_returns_the_process_pin_once(pin_redis):
    memory_recall.store_pinned("alice", ["  harbour log  ", ""])
    assert memory_recall.take_pinned("alice") == ["harbour log"]
    assert memory_recall.take_pinned("alice") == []
    assert pin_redis.data == {}


async def test_restart_serves_the_pin_once_and_a_new_pin_replaces_it(
    monkeypatch, pin_redis
):
    await pin(monkeypatch, "bob memory", [0], user="bob")
    await pin(monkeypatch, "old topic", [0])
    await pin(monkeypatch, "new topic", [0])
    assert set(pin_redis.ttls.values()) == {caches.RECALL_PIN_TTL}

    restart()
    assert await memory_recall.consume_pinned("alice") == ["new topic"]
    assert await memory_recall.consume_pinned("alice") == []
    assert await memory_recall.consume_pinned("bob") == ["bob memory"]
    assert await memory_recall.consume_pinned("bob") == []


async def test_failed_pick_does_not_keep_the_old_pin_across_a_restart(
    monkeypatch, pin_redis
):
    await pin(monkeypatch, "old topic", [0])
    await pin(monkeypatch, "new topic", [])
    restart()
    assert await memory_recall.consume_pinned("alice") == []
    assert caches._pin_key("alice") not in pin_redis.data


async def test_live_recall_reads_the_pin_without_the_picker(monkeypatch, pin_redis):
    await pin(monkeypatch, "harbour log", [0])
    restart()
    picker = AsyncMock(side_effect=AssertionError("picker on the live turn"))
    monkeypatch.setattr(memory_recall, "select", picker)
    memory = MemoryManager()
    memory.recall_episodes = AsyncMock(return_value=[{"summary": "postgres row"}])
    memory.get_relationship_facts = AsyncMock(return_value={})
    memory.recall_exchanges_with_recency = AsyncMock(return_value=[])

    episodes, facts, exchanges = await memory.recall_for_prompt("next", user_id="alice")

    assert episodes == ["harbour log", "postgres row"]
    assert facts == {} and exchanges == []
    assert memory.recall_episodes.await_args.kwargs["pick"] is False
    picker.assert_not_awaited()
    again, _, _ = await memory.recall_for_prompt("next", user_id="alice")
    assert again == ["postgres row"]
    assert pin_redis is not None


async def test_redis_down_keeps_the_process_pin_once(monkeypatch, pin_redis):
    pin_redis.down = True
    await pin(monkeypatch, "harbour log", [0])
    picker = AsyncMock(side_effect=AssertionError("picker on the live turn"))
    monkeypatch.setattr(memory_recall, "select", picker)
    memory = MemoryManager()
    memory.recall_episodes = AsyncMock(return_value=[{"summary": "postgres row"}])
    memory.get_relationship_facts = AsyncMock(return_value={})
    memory.recall_exchanges_with_recency = AsyncMock(return_value=[])

    episodes, _, _ = await memory.recall_for_prompt("next", user_id="alice")
    again, _, _ = await memory.recall_for_prompt("next", user_id="alice")

    assert episodes == ["harbour log", "postgres row"]
    assert again == ["postgres row"]
    picker.assert_not_awaited()
    assert caches._pin_key("alice") not in pin_redis.data


async def test_a_hung_redis_does_not_block_the_process_pin(monkeypatch, pin_redis):
    await pin(monkeypatch, "harbour log", [0])
    pin_redis.hang = True
    started = time.monotonic()
    assert await memory_recall.consume_pinned("alice") == ["harbour log"]
    assert time.monotonic() - started < 1.5
    assert await memory_recall.consume_pinned("alice") == []


async def test_pending_pick_does_not_revive_the_redis_pin(monkeypatch, pin_redis):
    await pin(monkeypatch, "old topic", [0])
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch(*_args, **_kwargs):
        started.set()
        await release.wait()
        return [episode("new topic")]

    monkeypatch.setattr(memory_recall, "fetch_candidates", fetch)
    monkeypatch.setattr(memory_recall, "select", AsyncMock(return_value=[0]))
    task = asyncio.create_task(MemoryManager().pin_next_recall("new topic", "alice"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        pin_redis.data[caches._pin_key("alice")] = '["old topic"]'
        assert await memory_recall.consume_pinned("alice") == []
    finally:
        release.set()
        await task

    restart()
    assert await memory_recall.consume_pinned("alice") == ["new topic"]


async def test_older_job_cannot_replace_the_newer_redis_pin(monkeypatch, pin_redis):
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch(_ranked, _recent, query, _user_id):
        if query == "old topic":
            started.set()
            await release.wait()
        return [episode(query)]

    monkeypatch.setattr(memory_recall, "fetch_candidates", fetch)
    monkeypatch.setattr(memory_recall, "select", AsyncMock(return_value=[0]))
    memory = MemoryManager()
    older = asyncio.create_task(memory.pin_next_recall("old topic", "alice"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await memory.pin_next_recall("new topic", "alice")
    finally:
        release.set()
        await older

    restart()
    assert await memory_recall.consume_pinned("alice") == ["new topic"]
    assert memory_recall.take_pinned("alice") == []


def _lua_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return '"' + escaped + '"'


def _run_lua(script, keys, args, store):
    init = ", ".join(f"[{_lua_quote(k)}]={_lua_quote(v)}" for k, v in store.items())
    key_lits = ", ".join(_lua_quote(key) for key in keys)
    arg_lits = ", ".join(_lua_quote(arg) for arg in args)
    program = f"""
local store = {{{init}}}
local ttls = {{}}
local function redis_call(cmd, ...)
  local a = {{...}}
  if cmd == 'GET' then
    local v = store[a[1]]
    if v == nil then return false end
    return v
  end
  if cmd == 'SET' then
    store[a[1]] = a[2]
    if a[3] == 'EX' then ttls[a[1]] = tostring(a[4]) end
    return 'OK'
  end
  if cmd == 'DEL' then
    store[a[1]] = nil
    ttls[a[1]] = nil
    return 1
  end
  error('bad cmd ' .. tostring(cmd))
end
redis = {{ call = redis_call }}
KEYS = {{ {key_lits} }}
ARGV = {{ {arg_lits} }}
local result = (function()
{script}
end)()
if result == nil or result == false then
  io.write("RESULT|nil\\n")
else
  io.write("RESULT|", tostring(result), "\\n")
end
for k, v in pairs(store) do
  io.write("KV|", k, "|", v, "|", ttls[k] or "", "\\n")
end
"""
    proc = subprocess.run(
        ["luajit", "-e", program],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    result = None
    data = {}
    ttls = {}
    for line in proc.stdout.splitlines():
        kind, _, rest = line.partition("|")
        if kind == "RESULT":
            result = None if rest == "nil" else int(rest) if rest.isdigit() else rest
        elif kind == "KV":
            key, value, ttl = rest.split("|", 2)
            data[key] = value
            if ttl:
                ttls[key] = int(float(ttl))
    return result, data, ttls


@pytest.mark.skipif(shutil.which("luajit") is None, reason="luajit is not installed")
def test_pin_scripts_keep_the_newer_generation_only():
    steps = [
        (caches._PIN_PUT, ["g", "p"], ["00000000000000000001", '["old"]', "600"], _put),
        (caches._PIN_PUT, ["g", "p"], ["00000000000000000002", '["new"]', "600"], _put),
        (caches._PIN_PUT, ["g", "p"], ["00000000000000000001", '["old"]', "600"], _put),
        (caches._PIN_DROP, ["g", "p"], ["00000000000000000001"], _drop),
        (caches._PIN_PUT, ["g", "p"], ["00000000000000000003", "", "600"], _put),
        (caches._PIN_PUT, ["g", "p"], ["00000000000000000002", '["old"]', "600"], _put),
        (caches._PIN_PUT, ["g", "p"], ["00000000000000000003", '["kept"]', "600"], _put),
        (caches._PIN_TAKE, ["p"], [], _take),
        (caches._PIN_TAKE, ["p"], [], _take),
    ]
    lua_store = {}
    py_store = FakeRedis()
    for script, keys, args, apply in steps:
        before = dict(lua_store)
        lua_result, lua_store, _ttls = _run_lua(script, keys, args, before)
        py_result = apply(py_store, keys, args)
        assert lua_result == py_result
        assert lua_store == py_store.data
        if script == caches._PIN_PUT and lua_result == 1:
            assert _ttls.get("g") == 600
    assert "p" not in lua_store
    assert lua_store["g"] == "00000000000000000003"
