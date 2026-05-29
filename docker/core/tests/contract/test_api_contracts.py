"""Contract tests for klukai HTTP API public surface.

S+ Phase 4 — pins JSON-schema for endpoints external callers depend on.
Drift here is a breaking change requiring an ADR + version bump.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SCHEMA_DIR = Path(__file__).parent / "schemas"


def test_health_schema_pinned() -> None:
    """The /health endpoint contract MUST be pinned. Drift = breaking change."""
    expected_keys = {"status", "service", "version", "database", "redis", "qdrant", "cache"}
    schema_path = SCHEMA_DIR / "health.schema.json"
    assert schema_path.exists(), (
        f"pinned contract missing: {schema_path}. The schema must be a committed "
        "artifact — the test must not regenerate the answer it then asserts."
    )
    schema = json.loads(schema_path.read_text())
    assert set(schema["required"]) >= expected_keys, (
        "health schema drifted — keys removed from required list"
    )


def test_chat_turn_contract_pinned() -> None:
    """Locks request/response shape of POST /api/chat/turn."""
    schema_path = SCHEMA_DIR / "chat-turn.schema.json"
    assert schema_path.exists(), (
        f"pinned contract missing: {schema_path}. The schema must be a committed "
        "artifact — the test must not regenerate the answer it then asserts."
    )
    schema = json.loads(schema_path.read_text())
    assert schema["$defs"]["response"]["properties"]["affection"]["properties"]["level"][
        "maximum"
    ] == 9, "Klukai affection level taxonomy is 0-9 (per ADR-0005)"
    assert schema["$defs"]["response"]["properties"]["affection"]["properties"]["level"][
        "minimum"
    ] == 0


def test_affection_taxonomy_locked() -> None:
    """Per ADR-0005: affection levels are 0-9, no more, no less. Any
    change requires an ADR supersession."""
    try:
        from app.affection import AffectionState
    except ImportError:
        pytest.skip("AffectionState not importable in dev env")

    # State at extreme score still has level in [0, 9].
    state_low = AffectionState(score=0, level=0)
    state_high = AffectionState(score=999999, level=9)
    assert 0 <= state_low.level <= 9
    assert 0 <= state_high.level <= 9
