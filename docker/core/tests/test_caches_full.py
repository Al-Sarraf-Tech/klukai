"""Tests for app.caches — Redis-backed embedding + affection caches."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app import caches


@pytest.fixture(autouse=True)
def _reset_redis():
    caches._redis = None
    yield
    caches._redis = None


class TestTextKey:
    def test_deterministic(self):
        k1 = caches._text_key("hello world")
        k2 = caches._text_key("hello world")
        assert k1 == k2

    def test_different_texts_different_keys(self):
        k1 = caches._text_key("hello")
        k2 = caches._text_key("world")
        assert k1 != k2

    def test_format_prefix(self):
        k = caches._text_key("x")
        assert k.startswith("cache:embed:")


class TestEmbeddingCache:
    @pytest.mark.asyncio
    async def test_get_returns_none_on_miss(self):
        r = AsyncMock()
        r.get = AsyncMock(return_value=None)
        with patch("app.caches._get", return_value=r):
            result = await caches.get_embedding("not-cached")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_vector_on_hit(self):
        import json as _j
        r = AsyncMock()
        r.get = AsyncMock(return_value=_j.dumps([0.1, 0.2, 0.3]))
        with patch("app.caches._get", return_value=r):
            result = await caches.get_embedding("hit")
        assert result == [0.1, 0.2, 0.3]

    @pytest.mark.asyncio
    async def test_get_returns_none_on_invalid_payload(self):
        r = AsyncMock()
        r.get = AsyncMock(return_value='"not a list"')
        with patch("app.caches._get", return_value=r):
            result = await caches.get_embedding("invalid")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_fail_soft_on_redis_error(self):
        with patch("app.caches._get", side_effect=RuntimeError("redis down")):
            result = await caches.get_embedding("x")
        assert result is None

    @pytest.mark.asyncio
    async def test_put_writes_json(self):
        r = AsyncMock()
        r.set = AsyncMock()
        with patch("app.caches._get", return_value=r):
            await caches.put_embedding("hello", [0.1, 0.2])
        r.set.assert_awaited_once()
        # Check the EX parameter is set
        call_kwargs = r.set.call_args.kwargs
        assert call_kwargs["ex"] == caches.EMBEDDING_TTL

    @pytest.mark.asyncio
    async def test_put_fail_soft(self):
        with patch("app.caches._get", side_effect=RuntimeError("redis down")):
            # Should not raise
            await caches.put_embedding("x", [1.0])


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


class TestCosine:
    def test_identical_vectors_return_1(self):
        result = caches.cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0])
        assert result == pytest.approx(1.0, abs=1e-6)

    def test_orthogonal_vectors_return_0(self):
        result = caches.cosine([1.0, 0.0], [0.0, 1.0])
        assert result == pytest.approx(0.0, abs=1e-6)

    def test_opposite_vectors_return_neg_1(self):
        result = caches.cosine([1.0, 0.0], [-1.0, 0.0])
        assert result == pytest.approx(-1.0, abs=1e-6)

    def test_unequal_length_returns_zero(self):
        # Function returns 0 on error
        result = caches.cosine([1.0, 0.0], [1.0])
        assert result == 0.0

    def test_zero_vector_returns_zero(self):
        result = caches.cosine([0.0, 0.0, 0.0], [1.0, 2.0, 3.0])
        assert result == 0.0
