"""Shared test settings for the amarillo ops suites."""

import sys

import pytest


@pytest.fixture(autouse=True)
def _no_lookup_pauses(monkeypatch):
    """The CI relay retries GitHub's flaky commit search with short pauses;
    tests exercise the retries without waiting for them."""
    for module in list(sys.modules.values()):
        if hasattr(module, "RUN_LOOKUP_PAUSE") and hasattr(module, "runs_for"):
            monkeypatch.setattr(module, "RUN_LOOKUP_PAUSE", 0)
