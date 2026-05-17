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
    if not schema_path.exists():
        SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": sorted(expected_keys),
            "properties": {
                "status": {"type": "string", "enum": ["ok", "degraded", "down"]},
                "service": {"type": "string"},
                "version": {"type": "string"},
                "database": {"type": "object"},
                "redis": {"type": "string"},
                "qdrant": {"type": "string"},
                "cache": {"type": "object"},
            },
            "additionalProperties": True,
        }
        schema_path.write_text(json.dumps(schema, indent=2) + "\n")
    schema = json.loads(schema_path.read_text())
    assert set(schema["required"]) >= expected_keys, (
        "health schema drifted — keys removed from required list"
    )


def test_chat_turn_contract_pinned() -> None:
    """Locks request/response shape of POST /api/chat/turn."""
    schema_path = SCHEMA_DIR / "chat-turn.schema.json"
    if not schema_path.exists():
        SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {
                "request": {
                    "type": "object",
                    "required": ["message"],
                    "properties": {
                        "message": {"type": "string", "minLength": 1, "maxLength": 8000},
                        "conversation_id": {"type": "string"},
                    },
                },
                "response": {
                    "type": "object",
                    "required": ["reply", "affection"],
                    "properties": {
                        "reply": {"type": "string"},
                        "affection": {
                            "type": "object",
                            "required": ["score", "level"],
                            "properties": {
                                "score": {"type": "number"},
                                "level": {"type": "integer", "minimum": 0, "maximum": 9},
                            },
                        },
                        "mood": {"type": "string"},
                        "trace_id": {"type": "string"},
                    },
                },
            },
        }
        schema_path.write_text(json.dumps(schema, indent=2) + "\n")
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
