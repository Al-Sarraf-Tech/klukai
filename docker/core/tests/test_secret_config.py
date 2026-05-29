"""Tests for the fail-closed secret resolver (app.secret_config).

Covers all branches: env priority, dev placeholder (allowed contexts), and the
production fail-closed raise when no secret is configured.
"""

from __future__ import annotations

import pytest

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


def test_raises_in_production_when_no_secret(monkeypatch):
    # Simulate production: dev secrets NOT allowed and nothing configured.
    monkeypatch.delenv("ONLY_SECRET", raising=False)
    monkeypatch.setattr(secret_config, "dev_secrets_allowed", lambda: False)
    with pytest.raises(RuntimeError, match="No secret configured for signed-urls"):
        secret_config.resolve_secret("ONLY_SECRET", purpose="signed-urls")


def test_dev_secrets_allowed_via_explicit_env(monkeypatch):
    monkeypatch.setenv("KLUKAI_ALLOW_DEV_SECRETS", "1")
    assert secret_config.dev_secrets_allowed() is True


def test_dev_secrets_allowed_under_pytest(monkeypatch):
    # No explicit override, but pytest is imported during the run, so the
    # non-production detection returns True here.
    monkeypatch.delenv("KLUKAI_ALLOW_DEV_SECRETS", raising=False)
    assert secret_config.dev_secrets_allowed() is True
