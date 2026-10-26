"""Tests for app.caches — Redis-backed affection cache."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app import caches


@pytest.fixture(autouse=True)
def _reset_redis():
    caches._redis = None
    yield
    caches._redis = None


class TestAffectionCache:
    @pytest.mark.asyncio
    async def test_get_returns_none_on_miss(self):
        r = AsyncMock()
        r.get = AsyncMock(return_value=None)
        with patch("app.caches._get", return_value=r):
            assert await caches.get_affection("alice") is None

    @pytest.mark.asyncio
    async def test_get_returns_dict_on_hit(self):
        import json as _j
        r = AsyncMock()
        r.get = AsyncMock(return_value=_j.dumps({"score": 500, "level": 5}))
        with patch("app.caches._get", return_value=r):
            result = await caches.get_affection("alice")
        assert result == {"score": 500, "level": 5}

    @pytest.mark.asyncio
    async def test_get_fail_soft(self):
        with patch("app.caches._get", side_effect=RuntimeError("down")):
            assert await caches.get_affection("alice") is None

    @pytest.mark.asyncio
    async def test_put_writes_with_short_ttl(self):
        r = AsyncMock()
        r.set = AsyncMock()
        with patch("app.caches._get", return_value=r):
            await caches.put_affection("alice", {"score": 500})
        assert r.set.call_args.kwargs["ex"] == caches.AFFECTION_TTL

    @pytest.mark.asyncio
    async def test_put_fail_soft(self):
        with patch("app.caches._get", side_effect=RuntimeError("down")):
            await caches.put_affection("alice", {"x": 1})

    @pytest.mark.asyncio
    async def test_invalidate_deletes_key(self):
        r = AsyncMock()
        r.delete = AsyncMock()
        with patch("app.caches._get", return_value=r):
            await caches.invalidate_affection("alice")
        r.delete.assert_awaited_once_with("cache:aff:alice")

    @pytest.mark.asyncio
    async def test_invalidate_fail_soft(self):
        with patch("app.caches._get", side_effect=RuntimeError("down")):
            await caches.invalidate_affection("alice")


class TestClientLifecycle:
    @pytest.mark.asyncio
    async def test_get_creates_one_client_lazily(self):
        client = AsyncMock()
        with patch("app.caches.aioredis.from_url", return_value=client) as from_url:
            assert await caches._get() is client
            assert await caches._get() is client
        from_url.assert_called_once_with(caches.REDIS_URL, decode_responses=True)

    @pytest.mark.asyncio
    async def test_close_releases_client_and_swallows_errors(self):
        client = AsyncMock()
        client.aclose = AsyncMock(side_effect=RuntimeError("already gone"))
        caches._redis = client
        await caches.close()
        client.aclose.assert_awaited_once()
        assert caches._redis is None
        await caches.close()  # idempotent when nothing is open
