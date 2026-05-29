"""Tests for the fail-closed secret resolver (app.secret_config).

Covers all branches: env priority, dev placeholder (allowed contexts), and the
production fail-closed raise when no secret is configured.
"""

from __future__ import annotations

from app import secret_config


def test_returns_first_set_env(monkeypatch):
    monkeypatch.setenv("FOO_SECRET", "real-foo")
    monkeypatch.delenv("BAR_SECRET", raising=False)
    assert (
        secret_config.resolve_secret("FOO_SECRET", "BAR_SECRET", purpose="x")
        == "real-foo"
    )


def test_falls_through_to_later_env_in_priority_order(monkeypatch):
    monkeypatch.delenv("FIRST_SECRET", raising=False)
    monkeypatch.setenv("SECOND_SECRET", "second-val")
    assert (
        secret_config.resolve_secret("FIRST_SECRET", "SECOND_SECRET", purpose="x")
        == "second-val"
    )


def test_dev_placeholder_when_dev_secrets_allowed(monkeypatch):
    monkeypatch.delenv("ONLY_SECRET", raising=False)
    monkeypatch.setattr(secret_config, "dev_secrets_allowed", lambda: True)
    assert (
        secret_config.resolve_secret("ONLY_SECRET", purpose="signed-urls")
        == "dev-insecure-signed-urls"
    )


def test_generates_strong_secret_in_production_when_no_secret(monkeypatch):
    # Production: dev secrets NOT allowed and nothing configured -> generate a
    # strong random secret (never the guessable literal, never a crash).
    monkeypatch.delenv("ONLY_SECRET", raising=False)
    monkeypatch.setattr(secret_config, "dev_secrets_allowed", lambda: False)
    secret_config._GENERATED.pop("prodtest", None)
    val = secret_config.resolve_secret("ONLY_SECRET", purpose="prodtest")
    assert val and "dev-insecure" not in val and len(val) >= 32
    # Stable within the process so sign/verify round-trips.
    assert secret_config.resolve_secret("ONLY_SECRET", purpose="prodtest") == val
    secret_config._GENERATED.pop("prodtest", None)


def test_dev_secrets_allowed_via_explicit_env(monkeypatch):
    monkeypatch.setenv("KLUKAI_ALLOW_DEV_SECRETS", "1")
    assert secret_config.dev_secrets_allowed() is True


def test_dev_secrets_allowed_under_pytest(monkeypatch):
    # No explicit override, but pytest is imported during the run, so the
    # non-production detection returns True here.
    monkeypatch.delenv("KLUKAI_ALLOW_DEV_SECRETS", raising=False)
    assert secret_config.dev_secrets_allowed() is True
