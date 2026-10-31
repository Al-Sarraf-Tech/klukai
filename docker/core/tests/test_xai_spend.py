"""xAI price math, redaction, and the shared daily ledger."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app import xai_spend as spend


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("KLUKAI_XAI_LEDGER", str(tmp_path / "spend.sqlite3"))
    monkeypatch.delenv("KLUKAI_XAI_API_KEY", raising=False)
    monkeypatch.delenv("KLUKAI_CHAT_BACKEND", raising=False)
    monkeypatch.delenv("KLUKAI_IMAGE_BACKEND", raising=False)
    for name in (
        "KLUKAI_XAI_USD_PER_M_INPUT",
        "KLUKAI_XAI_USD_PER_M_CACHED",
        "KLUKAI_XAI_USD_PER_M_OUTPUT",
        "KLUKAI_XAI_USD_PER_IMAGE",
        "KLUKAI_API_DAILY_USD_CAP",
    ):
        monkeypatch.delenv(name, raising=False)


def test_backend_defaults_to_the_cli_without_a_key(ledger):
    assert spend.chat_backend() == "cli"
    assert spend.image_backend() == "cli"


def test_backend_defaults_to_the_api_when_a_key_is_present(ledger, monkeypatch):
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    assert spend.chat_backend() == "api"
    assert spend.image_backend() == "api"
    monkeypatch.setenv("KLUKAI_CHAT_BACKEND", "cli")
    monkeypatch.setenv("KLUKAI_IMAGE_BACKEND", "nope")
    assert spend.chat_backend() == "cli"
    assert spend.image_backend() == "api"


def test_redact_removes_the_live_key_and_key_shaped_tokens(ledger, monkeypatch):
    secret = "xai-" + ("b" * 24)
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", secret)
    other = "xai-" + ("c" * 16)
    text = spend.redact(f"failed {secret} and {other}")
    assert secret not in text
    assert other not in text
    assert text.count("[redacted]") == 2


def test_chat_price_uses_the_three_rates_and_not_reasoning(ledger):
    # 20 fresh input, 80 cached, 10 output.
    assert spend.chat_usd(100, 80, 10) == pytest.approx(0.00014)
    # Cached tokens cannot exceed the input bill.
    assert spend.chat_usd(10, 50, 0) == pytest.approx(10 * 0.5 / 1_000_000)


def test_invalid_rates_and_cap_fall_back_to_the_defaults(ledger, monkeypatch):
    monkeypatch.setenv("KLUKAI_XAI_USD_PER_M_INPUT", "nope")
    monkeypatch.setenv("KLUKAI_XAI_USD_PER_IMAGE", "0")
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "-4")
    assert spend.rates()[0] == 2.0
    assert spend.image_usd() == 0.09
    assert spend.ticks_usd(37756000) == pytest.approx(0.0037756)
    assert spend.ticks_usd(True) is None
    assert spend.daily_cap() == 3.0
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "1.5")
    assert spend.daily_cap() == 1.5


def test_the_ledger_is_one_utc_day(ledger):
    morning = datetime(2026, 10, 31, 1, tzinfo=UTC)
    evening = datetime(2026, 10, 31, 23, tzinfo=UTC)
    next_day = datetime(2026, 11, 1, 0, tzinfo=UTC)
    assert spend.spent_today(morning) == 0.0
    assert spend.record("chat", 1.25, morning) == pytest.approx(1.25)
    assert spend.record("image", 0.04, evening) == pytest.approx(1.29)
    assert spend.spent_today(evening) == pytest.approx(1.29)
    assert spend.spent_today(next_day) == 0.0
    assert not spend.capped(next_day)
    with pytest.raises(ValueError):
        spend.record("chat", -1, morning)
    with pytest.raises(ValueError):
        spend.record("other", 1, morning)


def test_a_zero_cap_is_already_spent(ledger, monkeypatch):
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "0")
    assert spend.capped()


def test_redact_without_a_key_still_hides_key_shaped_tokens(ledger):
    token = "xai-" + ("d" * 12)
    assert token not in spend.redact(f"saw {token}")


def test_a_cap_that_is_not_a_number_stays_at_three_dollars(ledger, monkeypatch):
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "nope")
    assert spend.daily_cap() == 3.0


def test_the_default_ledger_lives_under_the_home_share(ledger, monkeypatch):
    monkeypatch.delenv("KLUKAI_XAI_LEDGER")
    assert spend.ledger_path().name == "xai-spend.sqlite3"
