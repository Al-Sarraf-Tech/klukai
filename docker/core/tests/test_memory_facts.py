"""Tests for app.memory MemoryManager — fact tier + session helpers."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest

from app import fact_store
from app.memory import MemoryManager


class FakeFactStore:
    """In-memory stand-in for app.fact_store with the same key semantics."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.ttls: dict[str, int | None] = {}
        self.stores: list[tuple[str, str, int | None]] = []
        self.fail = False

    async def store(self, key, value, ttl_seconds=None):
        if self.fail:
            raise RuntimeError("postgres down")
        self.stores.append((key, value, ttl_seconds))
        self.rows[key] = value
        self.ttls[key] = ttl_seconds

    async def insert_if_absent(self, key, value):
        if self.fail:
            raise RuntimeError("postgres down")
        if key in self.rows:
            return False
        self.stores.append((key, value, None))
        self.rows[key] = value
        return True

    async def recall(self, key):
        if self.fail:
            raise RuntimeError("postgres down")
        if key not in self.rows:
            return None
        return {"key": key, "value": self.rows[key], "ts": "2026-10-27 00:00:00+00"}

    async def recall_pattern(self, pattern):
        if self.fail:
            raise RuntimeError("postgres down")
        # SQL LIKE with the default escape character (backslash).
        parts, i = [], 0
        while i < len(pattern):
            ch = pattern[i]
            if ch == "\\" and i + 1 < len(pattern):
                parts.append(re.escape(pattern[i + 1]))
                i += 2
                continue
            parts.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
            i += 1
        rx = re.compile("^" + "".join(parts) + "$")
        return [
            {"key": k, "value": v, "ts": "2026-10-27 00:00:00+00"}
            for k, v in self.rows.items()
            if rx.match(k)
        ]


@pytest.fixture
def facts(monkeypatch):
    fake = FakeFactStore()
    for name in ("store", "insert_if_absent", "recall", "recall_pattern"):
        monkeypatch.setattr(fact_store, name, getattr(fake, name))
    return fake


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
    async def test_swallows_database_error(self, facts):
        facts.fail = True
        # Should not raise: facts sit on the live chat path.
        await MemoryManager().store_fact("key", "value", user_id="alice")

    @pytest.mark.asyncio
    async def test_stores_under_namespaced_key(self, facts):
        await MemoryManager().store_fact("birthday", "March 5", user_id="alice")
        assert facts.stores == [("companion:alice:birthday", "March 5", None)]

    @pytest.mark.asyncio
    async def test_ttl_passed_when_set(self, facts):
        await MemoryManager().store_fact("ephemeral", "data", ttl=3600)
        assert facts.stores == [("companion:jalsarraf:ephemeral", "data", 3600)]

    @pytest.mark.asyncio
    async def test_never_touches_the_aichat_data_service(self, facts):
        m = MemoryManager()
        assert not hasattr(m, "_http")
        await m.store_fact("k", "v", user_id="alice")
        assert await m.recall_fact("k", user_id="alice") == "v"


class TestRecallFact:
    @pytest.mark.asyncio
    async def test_returns_value_when_found(self, facts):
        facts.rows["companion:alice:k"] = "ok"
        assert await MemoryManager().recall_fact("k", "alice") == "ok"

    @pytest.mark.asyncio
    async def test_other_users_fact_is_not_returned(self, facts):
        facts.rows["companion:bob:milestone:first_image"] = "x"
        assert (
            await MemoryManager().recall_fact("milestone:first_image", "alice") is None
        )

    @pytest.mark.asyncio
    async def test_a_recorded_milestone_is_not_recorded_again(self, facts):
        facts.rows["companion:alice:milestone:first_image"] = (
            "2026-10-20T05:51:38+00:00"
        )
        assert (
            await MemoryManager().record_milestone("first_image", user_id="alice")
            is False
        )
        assert facts.stores == []  # the first date is never overwritten

    @pytest.mark.asyncio
    async def test_new_milestone_is_recorded_once_in_utc(self, facts):
        m = MemoryManager()
        assert await m.record_milestone("first_image", user_id="alice") is True
        assert await m.record_milestone("first_image", user_id="alice") is False
        [(key, value, ttl)] = facts.stores
        assert key == "companion:alice:milestone:first_image" and ttl is None
        assert value.endswith("+00:00")

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, facts):
        assert await MemoryManager().recall_fact("k", "alice") is None

    @pytest.mark.asyncio
    async def test_returns_none_on_database_error(self, facts):
        facts.fail = True
        assert await MemoryManager().recall_fact("k", "alice") is None


class TestPatternRecall:
    @pytest.mark.asyncio
    async def test_scoped_to_the_user_and_pattern(self, facts):
        facts.rows.update(
            {
                "companion:alice:rel:birthday": "March 5",
                "companion:alice:rel:joke:tea": "tea :: always tea",
                "companion:alice:milestone:first": "2026-01-01",
                "companion:bob:rel:birthday": "June 1",
            }
        )
        m = MemoryManager()
        assert await m.get_relationship_facts("alice") == {"birthday": "March 5"}
        assert await m.get_inside_jokes("alice") == [
            {"label": "tea", "note": "always tea"}
        ]
        assert await m.get_milestones("alice") == {"first": "2026-01-01"}

    @pytest.mark.asyncio
    async def test_fails_open_to_empty(self, facts):
        facts.fail = True
        assert await MemoryManager().recall_facts_by_pattern("rel:%", "alice") == []


class TestRelationshipFacts:
    @pytest.mark.asyncio
    async def test_get_relationship_facts_returns_dict(self):
        m = MemoryManager()
        with patch.object(
            m,
            "recall_facts_by_pattern",
            return_value=[
                {"key": "companion:alice:rel:birthday", "value": "March 5"},
                {"key": "companion:alice:rel:job", "value": "engineer"},
            ],
        ):
            facts = await m.get_relationship_facts("alice")
        assert isinstance(facts, dict)
        assert "birthday" in facts or "rel:birthday" in str(facts)

    @pytest.mark.asyncio
    async def test_set_relationship_fact_delegates_to_store_fact(self):
        m = MemoryManager()
        with patch.object(m, "store_fact", new=AsyncMock()) as sf:
            await m.set_relationship_fact("birthday", "March 5", "alice")
        sf.assert_called_once()


class TestMilestones:
    @pytest.mark.asyncio
    async def test_record_milestone_returns_true_on_new(self, facts):
        assert await MemoryManager().record_milestone("first_kiss", "alice") is True

    @pytest.mark.asyncio
    async def test_record_milestone_returns_false_on_duplicate(self, facts):
        facts.rows["companion:alice:milestone:first_kiss"] = "2026-01-01"
        assert await MemoryManager().record_milestone("first_kiss", "alice") is False
        assert facts.rows["companion:alice:milestone:first_kiss"] == "2026-01-01"

    @pytest.mark.asyncio
    async def test_record_milestone_fails_closed_on_database_error(self, facts):
        facts.fail = True
        assert await MemoryManager().record_milestone("first_kiss", "alice") is False


class TestLikeScoping:
    @pytest.mark.asyncio
    async def test_user_id_wildcards_cannot_reach_other_users(self, facts):
        facts.rows.update(
            {
                "companion:axb:rel:secret": "theirs",
                "companion:a_b:rel:mine": "ours",
                "companion:anyone:rel:x": "theirs too",
            }
        )
        m = MemoryManager()
        assert await m.get_relationship_facts("a_b") == {"mine": "ours"}
        assert await m.get_relationship_facts("%") == {}


class TestPurgeGuards:
    def test_purge_never_targets_rows_without_an_expiry(self):
        """Every predicate that selects rows to delete requires an expiry."""
        import inspect

        code = inspect.getsource(fact_store.purge_expired).split('"""')[2]
        assert code.count("expires_at IS NOT NULL") == 2
        assert "expires_at IS NULL" not in code
        assert fact_store.PURGE_MARGIN_SECONDS >= 86400
