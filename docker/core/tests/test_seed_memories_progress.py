"""Fail-closed progress tracking for the standalone memory seeder."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import seed_memories

# A reply is always stored after the message it answers.
REPLY = timedelta(seconds=30)


# ── Watermark decoding (JSONB arrives already decoded) ──────────────────────


def _conn_returning(row: tuple | None) -> MagicMock:
    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=row)
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=cursor)
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("2026-08-01T12:00:00+00:00", id="jsonb-decoded-iso-string"),
        pytest.param('"2026-08-01T12:00:00+00:00"', id="legacy-text-encoded-json"),
    ],
)
async def test_last_seeded_at_decodes_both_stored_forms(stored: str) -> None:
    conn = _conn_returning((stored,))

    got = await seed_memories._get_last_seeded_at(conn, "jalsarraf")

    assert got == datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_last_seeded_at_is_none_when_unset_or_garbage() -> None:
    assert await seed_memories._get_last_seeded_at(_conn_returning(None)) is None
    assert await seed_memories._get_last_seeded_at(_conn_returning(("not a date",))) is None


# ── Dedup by exchange fingerprint ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_check_is_exact_fingerprint_scoped_to_user() -> None:
    conn = _conn_returning((1,))

    assert await seed_memories._is_duplicate(conn, "claude", "seed:2026-08-01T12:00:00") is True

    sql, params = conn.execute.await_args.args
    assert "user_id = %s AND conversation_id = %s" in sql
    assert "LIKE" not in sql
    assert params == ("claude", "seed:2026-08-01T12:00:00")

    assert await seed_memories._is_duplicate(_conn_returning(None), "claude", "seed:x") is False


# ── Unusable annotation fails closed ────────────────────────────────────────


# ── Deadline ────────────────────────────────────────────────────────────────


def test_deadline_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEED_DEADLINE_SECONDS", raising=False)
    assert seed_memories._deadline_from_env() is None
    monkeypatch.setenv("SEED_DEADLINE_SECONDS", "soon")
    assert seed_memories._deadline_from_env() is None
    monkeypatch.setenv("SEED_DEADLINE_SECONDS", "0")
    assert seed_memories._deadline_from_env() is None
    monkeypatch.setenv("SEED_DEADLINE_SECONDS", "600")
    deadline = seed_memories._deadline_from_env()
    assert deadline is not None
    assert 590 < deadline - time.monotonic() <= 600
    assert seed_memories._past_deadline(None) is False
    assert seed_memories._past_deadline(time.monotonic() - 1) is True


class TestHardKill:
    """SEED_HARD_KILL_SECONDS arms SIGALRM: the distroless image has no
    coreutils `timeout`, so the in-container hard bound lives in the seeder."""

    def test_unset_arms_nothing(self, monkeypatch):
        import seed_memories

        monkeypatch.delenv("SEED_HARD_KILL_SECONDS", raising=False)
        alarm = []
        monkeypatch.setattr(seed_memories.signal, "alarm", alarm.append)
        assert seed_memories._arm_hard_kill() is None
        assert alarm == []

    @pytest.mark.parametrize("raw", ["abc", "0", "-5"])
    def test_invalid_or_non_positive_arms_nothing(self, monkeypatch, raw):
        import seed_memories

        monkeypatch.setenv("SEED_HARD_KILL_SECONDS", raw)
        alarm = []
        monkeypatch.setattr(seed_memories.signal, "alarm", alarm.append)
        assert seed_memories._arm_hard_kill() is None
        assert alarm == []

    def test_set_arms_sigalrm_with_the_default_action(self, monkeypatch):
        import seed_memories

        monkeypatch.setenv("SEED_HARD_KILL_SECONDS", "6600")
        alarm = []
        handlers = []
        monkeypatch.setattr(seed_memories.signal, "alarm", alarm.append)
        monkeypatch.setattr(
            seed_memories.signal, "signal", lambda sig, h: handlers.append((sig, h))
        )
        assert seed_memories._arm_hard_kill() == 6600
        assert alarm == [6600]
        assert handlers == [(seed_memories.signal.SIGALRM, seed_memories.signal.SIG_DFL)]


class TestProgress:
    """The watermark moves over the handled leading exchanges only."""

    def test_handled_prefix_stops_at_the_first_unfinished_exchange(self):
        assert seed_memories._handled_prefix(5, {0, 1, 2, 3, 4}, set(), set()) == 5
        assert seed_memories._handled_prefix(5, {0, 1, 2, 3, 4}, {1, 3}, {1}) == 3
        assert seed_memories._handled_prefix(5, {0, 1}, set(), set()) == 2
        assert seed_memories._handled_prefix(5, set(), set(), set()) == 0

    @pytest.mark.parametrize(("raw", "expected"), [("", 40), ("7", 7), ("abc", 40), ("0", 40)])
    def test_env_bound(self, monkeypatch, raw, expected):
        monkeypatch.setenv("SEED_MAX_EXCHANGES", raw)
        assert seed_memories._max_exchanges_from_env() == expected


@pytest.mark.asyncio
async def test_a_stale_id_marker_falls_back_to_the_timestamp() -> None:
    """An older seeder may move last_seeded_at without the id key; an id whose
    timestamp no longer matches is ignored."""
    async def execute(sql: str, params: tuple = ()) -> MagicMock:
        cursor = MagicMock()
        if str(params[0]).startswith("last_seeded_carry:"):
            value = {"created_at": "2026-07-01T00:00:00+00:00", "carry": "old"}
        else:
            value = "2026-08-01T12:00:00+00:00"
        cursor.fetchone = AsyncMock(return_value=(value,))
        return cursor

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=execute)
    mark = await seed_memories._get_watermark(conn, "claude")
    assert mark == (datetime(2026, 8, 1, 12, tzinfo=timezone.utc), None)


@pytest.mark.asyncio
async def test_set_watermark_writes_both_keys_in_one_commit() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.commit = AsyncMock()
    ts = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    await seed_memories._set_watermark(conn, ts, "u7", "claude")
    keys = [c.args[1][0] for c in conn.execute.await_args_list]
    assert keys == ["last_seeded_at:claude", "last_seeded_carry:claude"]
    assert '"carry": "u7"' in conn.execute.await_args_list[1].args[1][1]
    assert conn.execute.await_args_list[0].args[1][1] == '"2026-08-01T12:00:00+00:00"'
    assert conn.commit.await_count == 1
