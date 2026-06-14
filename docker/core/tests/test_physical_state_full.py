"""Tests for app.physical_state — pure helpers + PhysicalStateTracker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.physical_state import (
    PhysicalStateTracker,
    STATES,
    get_description,
    should_decay,
)


class TestStatesMap:
    def test_normal_has_no_decay(self):
        assert STATES["normal"]["decay_hours"] is None

    def test_normal_description_empty(self):
        assert STATES["normal"]["description"] == ""

    def test_all_states_have_required_keys(self):
        for state, info in STATES.items():
            assert "decay_hours" in info
            assert "description" in info

    def test_combat_states_present(self):
        for s in ["sore", "exhausted", "wounded", "energized"]:
            assert s in STATES


class TestGetDescription:
    def test_known_state(self):
        d = get_description("sore")
        assert d != ""
        assert "ache" in d.lower() or "muscle" in d.lower()

    def test_unknown_state_returns_normal_description(self):
        # Unknown states fall back to "normal" — which is empty string
        assert get_description("bogus") == ""

    def test_normal_returns_empty(self):
        assert get_description("normal") == ""


class TestShouldDecay:
    def test_normal_never_decays(self):
        assert should_decay("normal", datetime.now(timezone.utc) - timedelta(days=100)) is False

    def test_unknown_state_no_decay(self):
        assert should_decay("bogus", datetime.now(timezone.utc) - timedelta(days=100)) is False

    def test_sore_decays_after_4h(self):
        old = datetime.now(timezone.utc) - timedelta(hours=5)
        assert should_decay("sore", old) is True

    def test_sore_persists_within_4h(self):
        recent = datetime.now(timezone.utc) - timedelta(hours=3)
        assert should_decay("sore", recent) is False

    def test_wounded_decays_after_8h(self):
        old = datetime.now(timezone.utc) - timedelta(hours=9)
        assert should_decay("wounded", old) is True

    def test_wounded_persists_within_8h(self):
        recent = datetime.now(timezone.utc) - timedelta(hours=7)
        assert should_decay("wounded", recent) is False

    # Regression: physical_state_since is a TIMESTAMPTZ, so psycopg hands back a
    # tz-AWARE datetime. The old code did `datetime.now(timezone.utc) - since` (naive) and
    # crashed every chat turn ("can't subtract offset-naive and offset-aware
    # datetimes") once a non-normal state existed — the old code subtracted a
    # naive datetime.now(). Cover the aware path the earlier naive-only tests missed.
    def test_aware_since_does_not_crash_and_decays(self):
        old = datetime.now(timezone.utc) - timedelta(hours=5)
        assert should_decay("sore", old) is True

    def test_aware_since_persists_within_window(self):
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        assert should_decay("sore", recent) is False

    def test_naive_since_still_supported(self):
        old = datetime.now().replace(tzinfo=None) - timedelta(hours=5)
        assert should_decay("sore", old) is True


# ═══════════════════════════════════════════════════════════════════════════
# PhysicalStateTracker (cache-only paths — DB paths via integration tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestTracker:
    @pytest.mark.asyncio
    async def test_get_state_from_cache(self):
        t = PhysicalStateTracker()
        t._cache["alice"] = ("sore", datetime.now(timezone.utc), "test detail")
        state, desc = await t.get_state("alice")
        assert state == "sore"
        assert desc == "test detail"

    @pytest.mark.asyncio
    async def test_get_state_uses_canonical_desc_when_no_detail(self):
        t = PhysicalStateTracker()
        t._cache["alice"] = ("sore", datetime.now(timezone.utc), None)
        state, desc = await t.get_state("alice")
        assert state == "sore"
        assert "ache" in desc.lower() or "muscle" in desc.lower()

    @pytest.mark.asyncio
    async def test_decayed_state_resets_to_normal(self):
        t = PhysicalStateTracker()
        # Put a sore state in cache that's 10h old (well past 4h decay)
        old = datetime.now(timezone.utc) - timedelta(hours=10)
        t._cache["alice"] = ("sore", old, None)
        with patch("app.physical_state.get_conn_autocommit") as gc:
            # Mock context manager that auto-acks the INSERT
            conn = AsyncMock()
            conn.execute = AsyncMock()
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=conn)
            ctx.__aexit__ = AsyncMock()
            gc.return_value = ctx
            state, desc = await t.get_state("alice")
        assert state == "normal"
        assert desc == ""

    @pytest.mark.asyncio
    async def test_set_state_unknown_falls_back_to_normal(self):
        t = PhysicalStateTracker()
        with patch("app.physical_state.get_conn_autocommit") as gc:
            conn = AsyncMock()
            conn.execute = AsyncMock()
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=conn)
            ctx.__aexit__ = AsyncMock()
            gc.return_value = ctx
            await t.set_state("alice", "bogus_state")
        assert t._cache["alice"][0] == "normal"

    @pytest.mark.asyncio
    async def test_set_state_caches_value(self):
        t = PhysicalStateTracker()
        with patch("app.physical_state.get_conn_autocommit") as gc:
            conn = AsyncMock()
            conn.execute = AsyncMock()
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=conn)
            ctx.__aexit__ = AsyncMock()
            gc.return_value = ctx
            await t.set_state("alice", "sore", detail="custom detail")
        cached = t._cache["alice"]
        assert cached[0] == "sore"
        assert cached[2] == "custom detail"

    @pytest.mark.asyncio
    async def test_set_state_fails_soft_on_db_error(self):
        t = PhysicalStateTracker()
        with patch("app.physical_state.get_conn_autocommit", side_effect=RuntimeError("db down")):
            # Should not raise
            await t.set_state("alice", "sore")
        # Cache still populated even though DB failed
        assert t._cache["alice"][0] == "sore"

    @pytest.mark.asyncio
    async def test_on_mission_end_wounded(self):
        t = PhysicalStateTracker()
        with patch("app.physical_state.get_conn_autocommit") as gc:
            conn = AsyncMock(); conn.execute = AsyncMock()
            ctx = AsyncMock(); ctx.__aenter__ = AsyncMock(return_value=conn); ctx.__aexit__ = AsyncMock()
            gc.return_value = ctx
            await t.on_mission_end("alice", had_injury=True)
        assert t._cache["alice"][0] == "wounded"

    @pytest.mark.asyncio
    async def test_on_mission_end_sore(self):
        t = PhysicalStateTracker()
        with patch("app.physical_state.get_conn_autocommit") as gc:
            conn = AsyncMock(); conn.execute = AsyncMock()
            ctx = AsyncMock(); ctx.__aenter__ = AsyncMock(return_value=conn); ctx.__aexit__ = AsyncMock()
            gc.return_value = ctx
            await t.on_mission_end("alice", had_injury=False)
        assert t._cache["alice"][0] == "sore"

    @pytest.mark.asyncio
    async def test_on_time_of_day_late_night_sets_cold(self):
        t = PhysicalStateTracker()
        t._cache["alice"] = ("normal", datetime.now(timezone.utc), None)
        with patch("app.physical_state.get_conn_autocommit") as gc:
            conn = AsyncMock(); conn.execute = AsyncMock()
            ctx = AsyncMock(); ctx.__aenter__ = AsyncMock(return_value=conn); ctx.__aexit__ = AsyncMock()
            gc.return_value = ctx
            await t.on_time_of_day("alice", hour=3)
        assert t._cache["alice"][0] == "cold"

    @pytest.mark.asyncio
    async def test_on_time_of_day_morning_sets_energized(self):
        t = PhysicalStateTracker()
        t._cache["alice"] = ("normal", datetime.now(timezone.utc), None)
        with patch("app.physical_state.get_conn_autocommit") as gc:
            conn = AsyncMock(); conn.execute = AsyncMock()
            ctx = AsyncMock(); ctx.__aenter__ = AsyncMock(return_value=conn); ctx.__aexit__ = AsyncMock()
            gc.return_value = ctx
            await t.on_time_of_day("alice", hour=7)
        assert t._cache["alice"][0] == "energized"

    @pytest.mark.asyncio
    async def test_on_time_of_day_does_not_override_active_state(self):
        t = PhysicalStateTracker()
        t._cache["alice"] = ("wounded", datetime.now(timezone.utc), None)
        await t.on_time_of_day("alice", hour=3)
        # Still wounded — no override
        assert t._cache["alice"][0] == "wounded"

    @pytest.mark.asyncio
    async def test_on_long_conversation_normal_to_relaxed(self):
        t = PhysicalStateTracker()
        t._cache["alice"] = ("normal", datetime.now(timezone.utc), None)
        with patch("app.physical_state.get_conn_autocommit") as gc:
            conn = AsyncMock(); conn.execute = AsyncMock()
            ctx = AsyncMock(); ctx.__aenter__ = AsyncMock(return_value=conn); ctx.__aexit__ = AsyncMock()
            gc.return_value = ctx
            await t.on_long_conversation("alice")
        assert t._cache["alice"][0] == "relaxed"

    @pytest.mark.asyncio
    async def test_on_long_conversation_does_not_override_wounded(self):
        t = PhysicalStateTracker()
        t._cache["alice"] = ("wounded", datetime.now(timezone.utc), None)
        await t.on_long_conversation("alice")
        assert t._cache["alice"][0] == "wounded"
