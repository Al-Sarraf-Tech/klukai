"""Optional service outages stop admissions while preserving their fail-soft result."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app import caches, events, rate_limit
from app.circuit_breakers import State, get_breaker


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["voice_cache", "rate_limit", "events"])
async def test_optional_redis_boundary_opens_and_stops_network_work(
    monkeypatch, boundary
):
    breaker = get_breaker("redis")
    breaker.cfg.error_threshold = 2
    client = MagicMock()
    operation = AsyncMock(side_effect=OSError("offline"))
    if boundary == "voice_cache":
        client.get = operation
        monkeypatch.setattr(caches, "_redis", client)

        async def invoke():
            return await caches.get_voice_translation("remember this")

        expected = None
    elif boundary == "rate_limit":
        # The limiter's network work is one MULTI/EXEC transaction.
        from tests.test_rate_limit_full import _Pipe

        pipe = _Pipe([])
        pipe.execute = operation
        client.pipeline = MagicMock(return_value=pipe)
        monkeypatch.setattr(rate_limit, "_redis", client)

        async def invoke():
            return await rate_limit.check_and_consume("alice", "default")

        expected = (999, 0)
    else:
        client.publish = operation
        monkeypatch.setattr(events, "_redis", client)

        async def invoke():
            return await events.publish("notification")

        expected = 0
    for _ in range(3):
        assert await invoke() == expected
    assert breaker.state is State.OPEN
    assert operation.await_count == 2
