"""Reject unsafe monitoring policies and retain bounded useful tool sources."""

from __future__ import annotations

import json

import pytest

from app.observability import slos
from app.tool_result_text import _bounded_tool_text, _extract_tool_text


def valid_policy():
    return {
        "version": 1,
        "window_days": 30,
        "rollback": {
            "minimum_requests": 20,
            "consecutive_breaches": 3,
            "poll_seconds": 10,
        },
        "endpoints": [
            {
                "method": "GET",
                "route": "/health",
                "availability": 0.999,
                "p99_ms": 30,
                "runbook": "dependency-outage",
            }
        ],
    }


@pytest.mark.parametrize(
    "change", ["version", "window", "empty", "duplicate", "rollback"]
)
def test_invalid_policy_cannot_drive_automatic_rollback(tmp_path, change):
    value = valid_policy()
    if change == "version":
        value["version"] = 2
    elif change == "window":
        value["window_days"] = 0
    elif change == "empty":
        value["endpoints"] = []
    elif change == "duplicate":
        value["endpoints"].append(value["endpoints"][0].copy())
    else:
        value["rollback"]["minimum_requests"] = 0
    path = tmp_path / "slos.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        slos.load_policy(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("availability", 0),
        ("availability", 1),
        ("availability", float("nan")),
        ("availability", True),
        ("availability", "0.999"),
        ("p99_ms", 0),
        ("p99_ms", float("inf")),
        ("route", "relative"),
        ("method", ""),
        ("runbook", None),
    ],
)
def test_objectives_require_finite_numbers_and_nonempty_route_identity(field, value):
    policy = valid_policy()
    policy["endpoints"][0][field] = value
    with pytest.raises(ValueError):
        slos.endpoint_slos(policy)


def test_policy_for_unregistered_endpoint_is_rejected():
    with pytest.raises(ValueError, match="unregistered routes") as failure:
        slos.validate_routes(set())
    assert "/health" in str(failure.value) and "GET" in str(failure.value)


def test_non_text_tool_payloads_are_ignored_without_discarding_visible_text():
    result = {
        "content": [
            {"type": "image", "data": "binary"},
            None,
            42,
            "Visible text",
            {"text": "fallback text"},
        ]
    }
    assert _extract_tool_text(result) == "Visible text\nfallback text"


def test_oversized_source_targets_do_not_overflow_model_context():
    huge = "https://example.invalid/" + "z" * 1001
    text = _bounded_tool_text("a" * 8100 + "\n" + huge)
    assert len(text) <= 8000 and text.endswith("\n[Tool result truncated.]")
    assert "Source URLs from this result:" not in text and huge not in text


def test_source_appendix_has_independent_character_budget():
    urls = ["https://example.invalid/" + letter * 700 for letter in "abc"]
    text = _bounded_tool_text("x" * 8100 + "\n" + "\n".join(urls))
    assert len(text) <= 8000
    assert text.endswith("\n".join(urls[:2])) and urls[2] not in text
    assert "Source URLs from this result:" in text


def test_source_appendix_keeps_first_six_distinct_links_in_order():
    urls = [f"https://example.invalid/source-{i}" for i in range(7)]
    text = _bounded_tool_text("x" * 8100 + "\n" + "\n".join(urls))
    assert text.endswith("\n".join(urls[:6]))
    assert urls[6] not in text and len(text) <= 8000
