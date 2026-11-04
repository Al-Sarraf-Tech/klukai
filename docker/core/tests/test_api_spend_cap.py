"""One $5/day cap across xAI and Anthropic, held before each paid call.

Owner, 2026-11-04: "Haiku can go up to $5 then and do all your fixes". The
ledger day is UTC. A call reserves its ceiling first, so concurrent calls
cannot all pass the check, then settles its real cost.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from app import xai_spend


def test_haiku_usage_is_priced_from_every_token_class(monkeypatch):
    usage = {
        "input_tokens": 1_000_000,
        "cache_creation_input_tokens": 3_000_000,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 1_000_000,
            "ephemeral_1h_input_tokens": 1_000_000,
        },
        "cache_read_input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }
    # 0.10 input + 0.125 5m + (0.20 1h + 0.20 unlabelled at the 1h rate)
    # + 0.01 read + 0.50 output
    assert xai_spend.anthropic_usd(usage) == pytest.approx(1.135)
    assert xai_spend.anthropic_usd({"input_tokens": True, "output_tokens": -4}) == 0
    assert xai_spend.anthropic_usd(None) == 0
    monkeypatch.setenv("KLUKAI_ANTHROPIC_USD_PER_M_OUTPUT", "2")
    assert xai_spend.anthropic_usd({"output_tokens": 1_000_000}) == pytest.approx(2)


def test_estimates_are_ceilings():
    assert xai_spend.anthropic_estimate(3000, 1000) > xai_spend.anthropic_usd(
        {"input_tokens": 1000, "output_tokens": 1000}
    )
    assert xai_spend.chat_estimate(3000, 1000) > xai_spend.chat_usd(1000, 0, 1000)


def test_a_hold_counts_until_it_settles_to_the_real_cost(monkeypatch):
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "1")
    hold = xai_spend.reserve("anthropic", 0.5)
    assert hold is not None
    assert xai_spend.spent_today() == pytest.approx(0.5)
    assert xai_spend.settle(hold, 0.01) == pytest.approx(0.01)
    assert xai_spend.spent_today() == pytest.approx(0.01)
    with pytest.raises(ValueError):
        xai_spend.settle(hold, -1)
    with pytest.raises(ValueError):
        xai_spend.reserve("openai", 0.1)


def test_every_provider_shares_one_cap(monkeypatch):
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "0.10")
    xai_spend.record("chat", 0.06)
    assert xai_spend.reserve("anthropic", 0.05) is not None
    assert xai_spend.reserve("image", 0.09) is None
    assert xai_spend.reserve("chat", 0.01) is None
    assert xai_spend.capped()


def test_concurrent_calls_cannot_all_pass_the_cap(monkeypatch):
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "0.30")
    with ThreadPoolExecutor(max_workers=8) as pool:
        holds = list(pool.map(lambda _n: xai_spend.reserve("anthropic", 0.1), range(8)))
    assert sum(hold is not None for hold in holds) == 3
    assert xai_spend.spent_today() == pytest.approx(0.30)


def test_the_cap_resets_on_the_utc_day(monkeypatch):
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "0.10")
    yesterday = datetime.now(UTC) - timedelta(days=1)
    xai_spend.record("anthropic", 0.10, now=yesterday)
    assert xai_spend.capped(now=yesterday)
    assert not xai_spend.capped()
    assert xai_spend.reserve("anthropic", 0.05) is not None


def test_a_broken_ledger_does_not_block_or_raise(monkeypatch):
    import sqlite3

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(xai_spend, "reserve", locked)
    monkeypatch.setattr(xai_spend, "settle", locked)
    monkeypatch.setattr(xai_spend, "record", locked)
    assert xai_spend.try_reserve("chat", 0.1) == (True, None)
    assert xai_spend.try_settle(None, "chat", 0.1) is None
    assert xai_spend.try_settle(7, "chat", 0.1) is None


def test_try_settle_without_a_hold_records_the_charge():
    assert xai_spend.try_settle(None, "anthropic", 0.0) is None
    assert xai_spend.try_settle(None, "anthropic", 0.02) == pytest.approx(0.02)
    assert xai_spend.spent_today() == pytest.approx(0.02)
