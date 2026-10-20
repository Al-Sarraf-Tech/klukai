"""Every extracted turn sends her heart rate, right after the mood frame."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import memory_extraction as mx
from app.extraction_llm import CallReport
from app.heartbeat import heart_rate
from app.memory_extraction import ExtractionRuntime
from app.models import SessionState


def _result(**overrides):
    out = {
        "facts": [],
        "mood": "composed",
        "topics": [],
        "should_remember": False,
        "interaction": {"type": "neutral", "intensity": 5},
        "commander_details": {},
        "gift_item": None,
    }
    out.update(overrides)
    return out


@pytest.fixture
def rt(monkeypatch):
    monkeypatch.setattr(mx.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        "app.fact_extractor.extract_promises", AsyncMock(return_value={})
    )
    monkeypatch.setattr("app.op_brief.on_turn", AsyncMock())

    order: list[tuple] = []
    ws = MagicMock()
    for name in (
        "send",
        "send_proactive",
        "send_affection",
        "send_affection_level_change",
    ):
        setattr(ws, name, AsyncMock())

    async def send_mood(user_id, mood):
        order.append(("mood", mood))

    async def send_heartbeat(user_id, bpm, mood, intensity):
        order.append(("heartbeat", bpm, mood, intensity))

    async def send_spike(user_id, bpm, mood):
        order.append(("spike", bpm, mood))

    ws.send_mood = AsyncMock(side_effect=send_mood)
    ws.send_heartbeat = AsyncMock(side_effect=send_heartbeat)
    ws.send_heartbeat_spike = AsyncMock(side_effect=send_spike)

    affection = MagicMock()
    affection.get_state = AsyncMock(return_value=SimpleNamespace(level=5, score=500))
    affection.apply_classification = AsyncMock(
        return_value=SimpleNamespace(
            new_score=510,
            new_level=5,
            new_level_name="Trusted",
            delta=10,
            level_changed=False,
            level_direction="",
        )
    )
    memory = MagicMock()
    memory.set_relationship_fact = AsyncMock()
    memory.store_exchange = AsyncMock()
    memory.record_milestone = AsyncMock()
    proactive = MagicMock()
    proactive.record_first = AsyncMock()

    applied = {"value": True}

    async def apply_state(session, mood, user_id):
        if applied["value"]:
            await ws.send_mood(user_id, mood)
        return applied["value"]

    reports: dict[str, CallReport] = {}

    async def extract(*args, report=None, **kwargs):
        if report is not None:
            report.provider, report.model, report.outcome = (
                "anthropic",
                "claude-haiku-5-5",
                "ok",
            )
            reports["last"] = report
        return state["result"]

    state = {"result": _result()}
    runtime = ExtractionRuntime(
        memory,
        affection,
        proactive,
        ws,
        lambda _uid: None,
        AsyncMock(),
        AsyncMock(side_effect=extract),
        AsyncMock(return_value=None),
        apply_state,
        AsyncMock(return_value=False),
        lambda: {"affection": {}},
    )
    return SimpleNamespace(
        runtime=runtime,
        ws=ws,
        order=order,
        state=state,
        applied=applied,
        memory=memory,
        affection=affection,
        reports=reports,
    )


def _session():
    return SessionState(conversation_id="c1", turn_count=1)


async def test_heartbeat_follows_the_mood_frame(rt):
    rt.state["result"] = _result(
        mood="admiring", interaction={"type": "compliment", "intensity": 7}
    )
    await rt.runtime.process("u", "a", _session(), user_id="u1")
    assert rt.order[:2] == [
        ("mood", "admiring"),
        ("heartbeat", heart_rate("admiring", 7), "admiring", 7),
    ]
    assert heart_rate("admiring", 7) == 86


async def test_heartbeat_uses_the_mood_after_contagion(rt):
    # Distress pulls composed toward protective before anything is sent.
    rt.state["result"] = _result(
        mood="composed", interaction={"type": "sad", "intensity": 8}
    )
    await rt.runtime.process("u", "a", _session(), user_id="u1")
    assert ("heartbeat", heart_rate("protective", 8), "protective", 8) in rt.order


async def test_unreadable_intensity_is_the_midpoint(rt):
    rt.state["result"] = _result(
        mood="serene", interaction={"type": "warm", "intensity": "lots"}
    )
    await rt.runtime.process("u", "a", _session(), user_id="u1")
    rt.ws.send_heartbeat.assert_awaited_once_with("u1", 60, "serene", 5)
    # The same coercion lets affection still apply instead of failing the turn.
    rt.affection.apply_classification.assert_awaited_once_with("warm", 5, "u1")


async def test_stale_turn_sends_no_heartbeat(rt):
    rt.applied["value"] = False
    await rt.runtime.process("u", "a", _session(), user_id="u1")
    rt.ws.send_heartbeat.assert_not_awaited()
    rt.memory.store_exchange.assert_awaited_once()


async def test_heartbeat_send_failure_never_skips_affection_or_memory(rt):
    rt.ws.send_heartbeat.side_effect = RuntimeError("socket gone")
    await rt.runtime.process("u", "a", _session(), user_id="u1")
    rt.affection.apply_classification.assert_awaited_once()
    rt.memory.store_exchange.assert_awaited_once()


@pytest.mark.parametrize(
    ("mood", "bpm"), [("breathless", 162), ("overwhelmed", 158), ("heartbroken", 150)]
)
async def test_new_intense_moods_spike_after_the_heartbeat(rt, mood, bpm):
    rt.state["result"] = _result(
        mood=mood, interaction={"type": "flirty", "intensity": 9}
    )
    await rt.runtime.process("u", "a", _session(), user_id="u1")
    kinds = [entry[0] for entry in rt.order]
    assert kinds.index("mood") < kinds.index("heartbeat") < kinds.index("spike")
    rt.ws.send_heartbeat_spike.assert_awaited_once_with("u1", bpm, mood)


async def test_one_info_line_without_content(rt, caplog):
    rt.state["result"] = _result(
        mood="giddy", interaction={"type": "playful", "intensity": 6}
    )
    with caplog.at_level(logging.INFO, logger="app.memory_extraction"):
        await rt.runtime.process(
            "secret words", "private reply", _session(), user_id="u1"
        )
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Turn extraction:")
    ]
    assert lines == [
        "Turn extraction: provider=anthropic model=claude-haiku-5-5 outcome=ok "
        "mood=giddy intensity=6 bpm=107"
    ]
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "secret words" not in joined and "private reply" not in joined


async def test_unreported_extractor_logs_unknown_provider(rt, caplog):
    rt.runtime.extract_facts = AsyncMock(return_value=_result())
    with caplog.at_level(logging.INFO, logger="app.memory_extraction"):
        await rt.runtime.process("u", "a", _session(), user_id="u1")
    assert any(
        "provider=local model=- outcome=pending" in r.getMessage()
        for r in caplog.records
    )
