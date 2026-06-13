"""Tests for app.memory MemoryManager — fact tier + session helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory import MemoryManager


def _mk_http_resp(body=None, status=200):
    r = MagicMock()
    r.json = MagicMock(return_value=body or {})
    r.status_code = status
    r.raise_for_status = MagicMock()
    return r


class TestRedisOpReconnect:
    @pytest.mark.asyncio
    async def test_no_retry_on_success(self):
        m = MemoryManager()
        op = AsyncMock(return_value="hello")
        op.__name__ = "test_op"
        result = await m._redis_op(op, "k")
        assert result == "hello"
        op.assert_called_once_with("k")


class TestStoreFact:
    @pytest.mark.asyncio
    async def test_swallows_http_error(self):
        m = MemoryManager()
        m._http = AsyncMock()
        m._http.post = AsyncMock(side_effect=RuntimeError("data svc down"))
        # Should not raise
        await m.store_fact("key", "value", user_id="alice")

    @pytest.mark.asyncio
    async def test_posts_with_namespaced_key(self):
        m = MemoryManager()
        m._http = AsyncMock()
        m._http.post = AsyncMock(return_value=_mk_http_resp())
        await m.store_fact("birthday", "March 5", user_id="alice")
        # Body should namespace the key with companion:user_id:
        body = m._http.post.call_args.kwargs["json"]
        assert body["key"] == "companion:alice:birthday"
        assert body["value"] == "March 5"

    @pytest.mark.asyncio
    async def test_ttl_included_when_set(self):
        m = MemoryManager()
        m._http = AsyncMock()
        m._http.post = AsyncMock(return_value=_mk_http_resp())
        await m.store_fact("ephemeral", "data", ttl=3600)
        body = m._http.post.call_args.kwargs["json"]
        assert body["ttl_seconds"] == 3600

    @pytest.mark.asyncio
    async def test_ttl_omitted_when_none(self):
        m = MemoryManager()
        m._http = AsyncMock()
        m._http.post = AsyncMock(return_value=_mk_http_resp())
        await m.store_fact("permanent", "data")
        body = m._http.post.call_args.kwargs["json"]
        assert "ttl_seconds" not in body


class TestRecallFact:
    @pytest.mark.asyncio
    async def test_returns_value_when_found(self):
        m = MemoryManager()
        m._http = AsyncMock()
        m._http.get = AsyncMock(return_value=_mk_http_resp({"found": True, "value": "ok"}))
        result = await m.recall_fact("k", "alice")
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        m = MemoryManager()
        m._http = AsyncMock()
        m._http.get = AsyncMock(return_value=_mk_http_resp({"found": False}))
        result = await m.recall_fact("k", "alice")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self):
        m = MemoryManager()
        m._http = AsyncMock()
        m._http.get = AsyncMock(side_effect=RuntimeError("data svc down"))
        result = await m.recall_fact("k", "alice")
        assert result is None

    @pytest.mark.asyncio
    async def test_namespaces_key(self):
        m = MemoryManager()
        m._http = AsyncMock()
        m._http.get = AsyncMock(return_value=_mk_http_resp({"found": True, "value": "x"}))
        await m.recall_fact("birthday", "alice")
        params = m._http.get.call_args.kwargs["params"]
        assert params["key"] == "companion:alice:birthday"


class TestRelationshipFacts:
    @pytest.mark.asyncio
    async def test_get_relationship_facts_returns_dict(self):
        m = MemoryManager()
        m._http = AsyncMock()
        m._http.get = AsyncMock(return_value=_mk_http_resp({"results": [
            {"key": "companion:alice:rel:birthday", "value": "March 5"},
            {"key": "companion:alice:rel:job", "value": "engineer"},
        ]}))
        with patch.object(m, "recall_facts_by_pattern",
                          return_value=[
                              {"key": "companion:alice:rel:birthday", "value": "March 5"},
                              {"key": "companion:alice:rel:job", "value": "engineer"},
                          ]):
            facts = await m.get_relationship_facts("alice")
        assert isinstance(facts, dict)
        assert "birthday" in facts or "rel:birthday" in str(facts)

    @pytest.mark.asyncio
    async def test_set_relationship_fact_delegates_to_store_fact(self):
        m = MemoryManager()
        m._http = AsyncMock()
        m._http.post = AsyncMock(return_value=_mk_http_resp())
        with patch.object(m, "store_fact", new=AsyncMock()) as sf:
            await m.set_relationship_fact("birthday", "March 5", "alice")
        sf.assert_called_once()


class TestMilestones:
    @pytest.mark.asyncio
    async def test_record_milestone_returns_true_on_new(self):
        m = MemoryManager()
        m._http = AsyncMock()
        m._http.post = AsyncMock(return_value=_mk_http_resp())
        with patch.object(m, "recall_fact", new=AsyncMock(return_value=None)):
            ok = await m.record_milestone("first_kiss", "alice")
        assert ok is True

    @pytest.mark.asyncio
    async def test_record_milestone_returns_false_on_duplicate(self):
        m = MemoryManager()
        m._http = AsyncMock()
        m._http.post = AsyncMock(return_value=_mk_http_resp())
        # Already exists
        with patch.object(m, "recall_fact", new=AsyncMock(return_value="2026-01-01")):
            ok = await m.record_milestone("first_kiss", "alice")
        assert ok is False
