"""Tests for Her POV memory portraits."""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import memory_her_pov as hp


def test_is_trivial():
    assert hp._is_trivial("ok")
    assert hp._is_trivial("hi")
    assert not hp._is_trivial("Tell me about the last mission with Mechty.")


@pytest.mark.asyncio
async def test_pick_exchange_pairs_user_assistant():
    rows_chrono = [
        ("1", "user", "I brought coffee for once today commander.", "composed", "", None),
        ("2", "assistant", "...I noticed. Don't expect gratitude out loud.", "quietly_pleased", "m", None),
        ("3", "user", "ok", "composed", "", None),
        ("4", "assistant", "Mm.", "composed", "m", None),
        ("5", "user", "Remember when we outran the storm on the bike?", "composed", "", None),
        ("6", "assistant", "I remember holding the throttle. You held on without being told.", "tender", "m", None),
    ]
    desc = list(reversed(rows_chrono))

    class FakeResult:
        async def fetchall(self):
            return desc

    class FakeConn:
        async def execute(self, *a, **k):
            return FakeResult()

    @asynccontextmanager
    async def fake_get_conn():
        yield FakeConn()

    with patch("app.db.get_conn", fake_get_conn):
        result = await hp.pick_exchange("u1")

    assert result is not None
    blob = result["user_content"] + result["assistant_content"]
    assert ("coffee" in blob) or ("storm" in blob)


@pytest.mark.asyncio
async def test_compose_pov_fallback_on_bad_llm():
    exchange = {
        "user_content": "Hello there Commander test long enough",
        "assistant_content": "State your business.",
        "mood": "composed",
    }

    class _Gate:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
        with patch("app.llm_json.call_llm", AsyncMock(return_value={})):
            pov = await hp.compose_pov(exchange, affection_level=5)
    assert len(pov["annotation"]) >= 8
    assert len(pov["scene_tags"]) >= 8


@pytest.mark.asyncio
async def test_compose_pov_uses_llm_fields():
    exchange = {
        "user_content": "I waited for you at the hangar.",
        "assistant_content": "You did not have to.",
        "mood": "tender",
    }

    class _Gate:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    llm = {
        "annotation": "He waited. I pretended I arrived first.",
        "scene_tags": "hangar, night, silver hair, looking away",
        "couple": True,
        "mood": "tender",
        "title": "Hangar Wait",
    }
    with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
        with patch("app.llm_json.call_llm", AsyncMock(return_value=llm)):
            pov = await hp.compose_pov(exchange, affection_level=7)
    assert pov["annotation"].startswith("He waited")
    assert pov["couple"] is True
    assert pov["title"] == "Hangar Wait"


@pytest.mark.asyncio
async def test_start_her_pov_returns_job_id():
    with patch.object(hp, "run_her_pov", new=AsyncMock()):
        with patch("app.context.ws") as mock_ws:
            mock_ws.track_task = MagicMock()
            out = await hp.start_her_pov("claude")
    assert "job_id" in out
    job = await hp.get_job(out["job_id"])
    assert job is not None
    assert job["user_id"] == "claude"
