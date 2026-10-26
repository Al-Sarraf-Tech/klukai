"""A slow background step never holds back her mood, heart rate or affection.

Production, 2026-10-19 (bbaef74, extraction on Haiku): with Dominus off, the
still-local promise detection waited out a 120 s connect inside the turn's
background step, ahead of the mood and heartbeat, so both arrived minutes
late. Mood, heartbeat and affection now come straight after the extraction
result. Since 2026-10-26 every background call is Haiku-only (no local
fallback to wait for); the turn-order tests keep the ordering guarantee.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import chat
from app.memory import MemoryManager
from app.models import SessionState

from .test_session_authority import StoredRedis
from .test_turn_heartbeat import _result, _session, rt  # noqa: F401 (fixture)


# ── turn order ───────────────────────────────────────────────────────────


async def test_mood_heartbeat_and_affection_do_not_wait_for_promises(
    rt,  # noqa: F811
    monkeypatch,
):
    hang = asyncio.Event()  # never set: a local call that never returns

    async def stuck(*_args, **_kwargs):
        await hang.wait()

    monkeypatch.setattr("app.fact_extractor.extract_promises", stuck)
    rt.state["result"] = _result(
        mood="tender",
        interaction={"type": "warm", "intensity": 6},
        facts=[{"key": "drink", "value": "coffee"}],
    )
    task = asyncio.create_task(
        rt.runtime.process("I'll call you later.", "a", _session(), user_id="u1")
    )
    try:
        # (The fixture stubs asyncio.sleep, so give the task real loop time.)
        done, _ = await asyncio.wait({task}, timeout=0.5)
        assert not done
        assert [entry[0] for entry in rt.order[:2]] == ["mood", "heartbeat"]
        rt.ws.send_heartbeat.assert_awaited_once()
        rt.affection.apply_classification.assert_awaited_once_with("warm", 6, "u1")
        rt.ws.send_affection.assert_awaited_once()
        # Facts are stored too; only the stuck promise step and what follows wait.
        rt.memory.set_relationship_fact.assert_awaited()
        rt.memory.store_exchange.assert_not_awaited()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_mood_lands_before_the_bookkeeping(rt, monkeypatch):  # noqa: F811
    seen: list[str] = []
    rt.ws.send_affection.side_effect = lambda *a, **k: seen.append("affection")
    rt.memory.set_relationship_fact.side_effect = lambda *a, **k: seen.append("fact")

    async def promises(*_a, **_k):
        seen.append("promises")
        return {}

    monkeypatch.setattr("app.fact_extractor.extract_promises", promises)
    rt.memory.store_exchange.side_effect = lambda *a, **k: seen.append("exchange")
    rt.state["result"] = _result(facts=[{"key": "k", "value": "v"}])
    await rt.runtime.process("u", "a", _session(), user_id="u1")
    assert rt.order[0][0] == "mood"
    assert seen == ["affection", "fact", "promises", "exchange"]


async def test_session_storage_failure_still_keeps_the_turns_record(
    rt,  # noqa: F811
):
    async def broken(*_a, **_k):
        raise RuntimeError("redis offline")

    rt.runtime.apply_state = broken
    rt.state["result"] = _result(facts=[{"key": "k", "value": "v"}])
    await rt.runtime.process("u", "a", _session(), user_id="u1")
    rt.ws.send_heartbeat.assert_not_awaited()
    rt.affection.apply_classification.assert_awaited_once()
    rt.memory.set_relationship_fact.assert_awaited()
    rt.memory.store_exchange.assert_awaited_once()


# ── compaction never makes a turn look stale ─────────────────────────────


@pytest.fixture
def store(monkeypatch):
    from app import background

    manager = MemoryManager()
    manager._redis = StoredRedis()
    monkeypatch.setattr(chat, "memory", manager)
    monkeypatch.setattr(background, "memory", manager)
    monkeypatch.setattr(background, "ws", AsyncMock())
    monkeypatch.setattr(background, "proactive", MagicMock())
    monkeypatch.setattr(
        background, "get_conn_autocommit", MagicMock(side_effect=RuntimeError("db"))
    )
    monkeypatch.setattr(background.context, "COMPACT_THRESHOLD", 6)
    monkeypatch.setattr(background.context, "COMPACT_KEEP_RAW", 2)

    async def compact(_turns):
        return "summary"

    monkeypatch.setattr("app.fact_extractor.compact_turns", compact)
    return manager


def _turn_session():
    return SessionState(
        conversation_id="c",
        revision=3,
        turns=[
            {"role": "user" if n % 2 == 0 else "assistant", "content": str(n)}
            for n in range(8)
        ],
    )


@pytest.mark.parametrize("compaction_first", [True, False])
async def test_compaction_and_extraction_both_apply(store, compaction_first):
    from app import background

    sid = chat.session_id("alice")
    turn = _turn_session()
    await store.save_session(sid, turn)
    snapshot = turn.model_copy(deep=True)
    if compaction_first:
        await background.background_compaction(turn.model_copy(deep=True), "alice")
        assert await background._apply_extracted_state(snapshot, "tender", "alice")
    else:
        assert await background._apply_extracted_state(snapshot, "tender", "alice")
        await background.background_compaction(turn.model_copy(deep=True), "alice")
    live = await store.get_session(sid)
    assert live.revision == 3
    assert live.mood == "tender"
    assert live.context_summary == "summary"
    assert [t["content"] for t in live.turns] == ["6", "7"]
    background.ws.send_mood.assert_awaited_once_with("alice", "tender")


async def test_a_newer_turn_still_wins(store):
    from app import background

    sid = chat.session_id("alice")
    turn = _turn_session()
    snapshot = turn.model_copy(deep=True)
    newer = turn.model_copy(deep=True)
    newer.revision = 4
    await store.save_session(sid, newer)
    assert not await background._apply_extracted_state(snapshot, "tender", "alice")
    assert (await store.get_session(sid)).mood == "composed"
