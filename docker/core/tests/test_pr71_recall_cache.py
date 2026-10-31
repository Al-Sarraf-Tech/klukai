"""A newer recall job owns the next pick, even when older work finishes late."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app import memory_recall
from app.memory import MemoryManager


def episode(text):
    return ("episode-id", text, [], [], 0.5, None)


@pytest.fixture(autouse=True)
def isolated_pins(monkeypatch):
    monkeypatch.setattr(memory_recall, "_PINNED", {})
    monkeypatch.setattr(memory_recall, "_PENDING_PINS", {})


@pytest.mark.parametrize(
    "outcome", ["no_rows", "empty", "unavailable", "fetch_error", "pick_error"]
)
async def test_new_unsuccessful_pick_drops_the_previous_pin(monkeypatch, outcome):
    memory_recall.store_pinned("alice", ["old topic"])
    fetch = AsyncMock(return_value=[] if outcome == "no_rows" else [episode("new topic")])
    pick = AsyncMock(return_value=None if outcome == "unavailable" else [])
    if outcome == "fetch_error":
        fetch.side_effect = RuntimeError("database unavailable")
    if outcome == "pick_error":
        pick.side_effect = RuntimeError("selection unavailable")
    monkeypatch.setattr(memory_recall, "fetch_candidates", fetch)
    monkeypatch.setattr(memory_recall, "select", pick)

    await MemoryManager().pin_next_recall("new question", "alice")

    assert memory_recall.take_pinned("alice") == []
    assert not memory_recall._PENDING_PINS


async def test_new_pick_invalidates_previous_pin_before_fetch_finishes(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    memory_recall.store_pinned("alice", ["old topic"])

    async def fetch(*_args):
        started.set()
        await release.wait()
        return []

    monkeypatch.setattr(memory_recall, "fetch_candidates", fetch)
    task = asyncio.create_task(MemoryManager().pin_next_recall("new question", "alice"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert memory_recall.take_pinned("alice") == []
    finally:
        release.set()
        await task


@pytest.mark.parametrize("latest_pick", [[0], []])
async def test_older_completion_cannot_replace_the_latest_result(monkeypatch, latest_pick):
    older_started, release_older = asyncio.Event(), asyncio.Event()

    async def fetch(_ranked, _recent, query, _user_id):
        if query == "old topic":
            older_started.set()
            await release_older.wait()
        return [episode(query)]

    async def pick(_caller, _subject, query, _texts, _limit, **_kwargs):
        return latest_pick if query == "new topic" else [0]

    monkeypatch.setattr(memory_recall, "fetch_candidates", fetch)
    monkeypatch.setattr(memory_recall, "select", pick)
    memory = MemoryManager()
    older = asyncio.create_task(memory.pin_next_recall("old topic", "alice"))
    try:
        await asyncio.wait_for(older_started.wait(), 1)
        await memory.pin_next_recall("new topic", "alice")
    finally:
        release_older.set()
        await older

    assert memory_recall.take_pinned("alice") == (["new topic"] if latest_pick else [])
    assert not memory_recall._PENDING_PINS


async def test_cancelled_older_job_preserves_newer_job_and_other_user(monkeypatch):
    older_started, newer_started = asyncio.Event(), asyncio.Event()
    release_newer = asyncio.Event()
    memory_recall.store_pinned("bob", ["bob's private memory"])

    async def fetch(_ranked, _recent, query, _user_id):
        if query == "old topic":
            older_started.set()
            await asyncio.Event().wait()
        newer_started.set()
        await release_newer.wait()
        return [episode(query)]

    monkeypatch.setattr(memory_recall, "fetch_candidates", fetch)
    monkeypatch.setattr(memory_recall, "select", AsyncMock(return_value=[0]))
    memory = MemoryManager()
    older = asyncio.create_task(memory.pin_next_recall("old topic", "alice"))
    newer = None
    try:
        await asyncio.wait_for(older_started.wait(), 1)
        newer = asyncio.create_task(memory.pin_next_recall("new topic", "alice"))
        await asyncio.wait_for(newer_started.wait(), 1)
        older.cancel()
        with pytest.raises(asyncio.CancelledError):
            await older
    finally:
        older.cancel()
        await asyncio.gather(older, return_exceptions=True)
        release_newer.set()
        if newer is not None:
            await newer

    assert memory_recall.take_pinned("alice") == ["new topic"]
    assert memory_recall.take_pinned("bob") == ["bob's private memory"]
    assert not memory_recall._PENDING_PINS


async def test_cancelled_current_job_clears_its_pin_and_ownership(monkeypatch):
    started = asyncio.Event()
    memory_recall.store_pinned("alice", ["old topic"])

    async def fetch(*_args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(memory_recall, "fetch_candidates", fetch)
    task = asyncio.create_task(MemoryManager().pin_next_recall("new topic", "alice"))
    try:
        await asyncio.wait_for(started.wait(), 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert memory_recall.take_pinned("alice") == []
    assert not memory_recall._PENDING_PINS
