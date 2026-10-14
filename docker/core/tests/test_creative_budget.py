"""Cloud creative attempts are bounded before any billable dispatch."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    from app import creative_budget as budget

    path = tmp_path / "private" / "attempts.sqlite3"
    monkeypatch.setenv("KLUKAI_CREATIVE_LEDGER", str(path))
    for kind in ("IMAGE", "TEXT"):
        monkeypatch.setenv(f"KLUKAI_CLOUD_{kind}_DAILY_LIMIT", "3")
        monkeypatch.setenv(f"KLUKAI_CLOUD_{kind}_MONTHLY_LIMIT", "5")
    monkeypatch.setattr(budget, "_utc_now", lambda: datetime(2026, 10, 14, tzinfo=timezone.utc))
    return budget, path


async def test_zero_or_invalid_limits_deny_without_creating_ledger(ledger, monkeypatch):
    budget, path = ledger
    for value in ("0", "-1", "garbage", "1.5", ""):
        monkeypatch.setenv("KLUKAI_CLOUD_IMAGE_DAILY_LIMIT", value)
        with pytest.raises(budget.CreativeBudgetError):
            await budget.reserve_attempt("image")
    assert not path.exists()


async def test_unset_limits_default_to_no_paid_requests(ledger, monkeypatch):
    budget, path = ledger
    monkeypatch.delenv("KLUKAI_CLOUD_IMAGE_MONTHLY_LIMIT")
    with pytest.raises(budget.CreativeBudgetError):
        await budget.reserve_attempt("image")
    assert not path.exists()


async def test_unknown_kind_and_nonpersistent_path_fail_closed(ledger, monkeypatch):
    budget, _ = ledger
    with pytest.raises(budget.CreativeBudgetError):
        await budget.reserve_attempt("video")
    for path in (":memory:", "relative.sqlite3", ""):
        monkeypatch.setenv("KLUKAI_CREATIVE_LEDGER", path)
        with pytest.raises(budget.CreativeBudgetError):
            await budget.reserve_attempt("image")


async def test_atomic_cap_across_independent_connections(ledger):
    budget, path = ledger
    results = await asyncio.gather(*(budget.reserve_attempt("image") for _ in range(15)), return_exceptions=True)
    accepted = [r for r in results if isinstance(r, str)]
    assert len(accepted) == 3
    assert len(set(accepted)) == 3
    assert all(isinstance(r, (str, budget.CreativeBudgetError)) for r in results)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM creative_attempts").fetchone()[0] == 3
    # A later caller gets the same persistent cap; no process-local counter.
    with pytest.raises(budget.CreativeBudgetError):
        await budget.reserve_attempt("image")


async def test_daily_reset_preserves_monthly_cap_and_kinds_are_separate(ledger, monkeypatch):
    budget, _ = ledger
    for _ in range(3):
        await budget.reserve_attempt("image")
    await budget.reserve_attempt("text")
    monkeypatch.setattr(budget, "_utc_now", lambda: datetime(2026, 10, 15, tzinfo=timezone.utc))
    for _ in range(2):
        await budget.reserve_attempt("image")
    with pytest.raises(budget.CreativeBudgetError):
        await budget.reserve_attempt("image")
    monkeypatch.setattr(budget, "_utc_now", lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert await budget.reserve_attempt("image")


async def test_storage_failure_denies_attempt(ledger, monkeypatch, tmp_path):
    budget, _ = ledger
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied")
    monkeypatch.setenv("KLUKAI_CREATIVE_LEDGER", str(parent_file / "ledger"))
    with pytest.raises(budget.CreativeBudgetError):
        await budget.reserve_attempt("text")


async def test_numeric_usage_only_and_failed_attempts_still_count(ledger):
    budget, path = ledger
    attempt = await budget.reserve_attempt("image")
    await budget.record_usage(attempt, {"input_tokens": 10, "output_tokens": 20,
                                       "input_tokens_details": {"text_tokens": 8, "image_tokens": 2},
                                       "prompt": "private", "api_key": "secret"})
    with closing(sqlite3.connect(path)) as conn:
        usage = conn.execute("SELECT usage_json FROM creative_attempts WHERE id=?", (attempt,)).fetchone()[0]
    assert '"output_tokens":20' in usage
    assert "private" not in usage and "secret" not in usage
    # Two more reservations consume the cap, regardless of recorded success.
    await budget.reserve_attempt("image")
    await budget.reserve_attempt("image")
    with pytest.raises(budget.CreativeBudgetError):
        await budget.reserve_attempt("image")


async def test_usage_recording_closes_its_database_connection(ledger, monkeypatch):
    budget, _ = ledger
    attempt = await budget.reserve_attempt("text")
    original_connect = sqlite3.connect
    connections = []

    def connect(*args, **kwargs):
        kwargs["check_same_thread"] = False
        conn = original_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(budget.sqlite3, "connect", connect)
    await budget.record_usage(attempt, {"input_tokens": 1})
    assert len(connections) == 1
    try:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[0].execute("SELECT 1")
    finally:
        connections[0].close()
