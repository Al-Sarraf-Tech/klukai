"""Tests for app/caches.py — embedding cache, affection cache, cosine, dedup."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestEmbeddingCache:
    @pytest.mark.asyncio
    async def test_miss_returns_none(self):
        from app.caches import get_embedding
        fake = AsyncMock()
        fake.get = AsyncMock(return_value=None)
        with patch("app.caches._get", return_value=fake):
            result = await get_embedding("hello")
        assert result is None

    @pytest.mark.asyncio
    async def test_hit_returns_vector(self):
        from app.caches import get_embedding
        vector = [0.1, 0.2, 0.3]
        fake = AsyncMock()
        fake.get = AsyncMock(return_value=json.dumps(vector))
        with patch("app.caches._get", return_value=fake):
            result = await get_embedding("hello")
        assert result == vector

    @pytest.mark.asyncio
    async def test_rejects_non_vector_payload(self):
        """Cache got polluted — return None rather than crashing."""
        from app.caches import get_embedding
        fake = AsyncMock()
        fake.get = AsyncMock(return_value=json.dumps({"not": "a vector"}))
        with patch("app.caches._get", return_value=fake):
            result = await get_embedding("hello")
        assert result is None

    @pytest.mark.asyncio
    async def test_put_serializes_and_sets_ttl(self):
        from app.caches import put_embedding, EMBEDDING_TTL
        fake = AsyncMock()
        fake.set = AsyncMock()
        with patch("app.caches._get", return_value=fake):
            await put_embedding("hello", [0.1, 0.2])
        fake.set.assert_awaited_once()
        kwargs = fake.set.call_args[1]
        assert kwargs.get("ex") == EMBEDDING_TTL

    @pytest.mark.asyncio
    async def test_put_silent_on_redis_failure(self):
        """Redis write failure must not propagate."""
        from app.caches import put_embedding

        async def broken():
            raise RuntimeError("down")

        with patch("app.caches._get", side_effect=broken):
            await put_embedding("x", [1.0])  # no raise

    @pytest.mark.asyncio
    async def test_identical_text_hashes_to_same_key(self):
        """Two calls with same text must hit the same Redis key."""
        from app.caches import _text_key
        k1 = _text_key("same text")
        k2 = _text_key("same text")
        assert k1 == k2

    def test_different_text_different_key(self):
        from app.caches import _text_key
        assert _text_key("abc") != _text_key("xyz")


class TestAffectionCache:
    @pytest.mark.asyncio
    async def test_get_hit(self):
        from app.caches import get_affection
        snap = {"score": 500, "level": 5}
        fake = AsyncMock()
        fake.get = AsyncMock(return_value=json.dumps(snap))
        with patch("app.caches._get", return_value=fake):
            result = await get_affection("alice")
        assert result == snap

    @pytest.mark.asyncio
    async def test_get_miss(self):
        from app.caches import get_affection
        fake = AsyncMock()
        fake.get = AsyncMock(return_value=None)
        with patch("app.caches._get", return_value=fake):
            result = await get_affection("nobody")
        assert result is None

    @pytest.mark.asyncio
    async def test_put_short_ttl(self):
        from app.caches import put_affection, AFFECTION_TTL
        fake = AsyncMock()
        fake.set = AsyncMock()
        with patch("app.caches._get", return_value=fake):
            await put_affection("alice", {"score": 100})
        assert fake.set.call_args[1]["ex"] == AFFECTION_TTL

    @pytest.mark.asyncio
    async def test_invalidate_calls_delete(self):
        from app.caches import invalidate_affection
        fake = AsyncMock()
        fake.delete = AsyncMock()
        with patch("app.caches._get", return_value=fake):
            await invalidate_affection("alice")
        fake.delete.assert_awaited_once()
        assert "alice" in fake.delete.call_args[0][0]


class TestCosine:
    def test_identical_vectors_return_one(self):
        from app.caches import cosine
        v = [1.0, 0.0, 0.0]
        assert abs(cosine(v, v) - 1.0) < 1e-9

    def test_orthogonal_return_zero(self):
        from app.caches import cosine
        assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_empty_returns_zero(self):
        from app.caches import cosine
        assert cosine([], []) == 0.0

    def test_mismatched_lengths_return_zero(self):
        from app.caches import cosine
        assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    def test_zero_vector_returns_zero(self):
        from app.caches import cosine
        assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


class TestIsDuplicateVector:
    @pytest.mark.asyncio
    async def test_returns_true_above_threshold(self):
        from app.caches import is_duplicate_vector

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json = MagicMock(return_value={"result": [{"score": 0.98}]})

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def post(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await is_duplicate_vector([0.1], "alice", threshold=0.95)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_below_threshold(self):
        from app.caches import is_duplicate_vector
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json = MagicMock(return_value={"result": [{"score": 0.5}]})

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def post(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await is_duplicate_vector([0.1], "alice", threshold=0.95)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_http_error(self):
        from app.caches import is_duplicate_vector

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def post(self, *a, **kw):
                raise RuntimeError("qdrant down")

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await is_duplicate_vector([0.1], "alice")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_non_200(self):
        from app.caches import is_duplicate_vector
        fake_resp = MagicMock()
        fake_resp.status_code = 500
        fake_resp.json = MagicMock(return_value={})

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def post(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await is_duplicate_vector([0.1], "alice")
        assert result is False
