"""Shared performance evidence and deterministic coverage contract."""

from __future__ import annotations
import hashlib
import json
import math
from typing import Any

# Voice is not measured: since 2026-10-26 the core speaks only through the paid
# Fish Audio API at a hardcoded endpoint, and CI never calls a paid provider.
TARGETS: tuple[tuple[str, str, int], ...] = (
    ("GET", "/health", 200),
    ("GET", "/api/health/live", 200),
    ("GET", "/api/health/ready", 200),
    ("POST", "/api/auth/login", 200),
    ("GET", "/api/auth/verify", 200),
    ("GET", "/api/affection", 200),
    ("GET", "/api/messages", 200),
    ("GET", "/api/memories", 200),
)
MIN_SAMPLES = 100
MIN_ROUNDS = 3


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def number(value: object, *, positive: bool = True) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ValueError("Nonfinite or invalid numeric performance evidence")
    if positive and value <= 0:
        raise ValueError("Nonpositive numeric performance evidence")
    return float(value)


def integer(value: object, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("Invalid positive integer performance evidence")
    return value


def object_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError("Expected an evidence object")
    return value


def object_list(value: object) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ValueError("Empty or invalid performance samples")
    return value
