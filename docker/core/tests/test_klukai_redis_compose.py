"""Klukai's own Redis (aichat decoupling, 2026-10-27): compose wiring.

Core and the events bridge must both use companion-redis with the password
from the encrypted compose env; the service must stay unpublished, capped,
and unable to be flushed or reconfigured over the wire.
"""

from __future__ import annotations

import re
from pathlib import Path

import redis.asyncio as redis

REPO = Path(__file__).resolve().parents[3]
COMPOSE = (REPO / "docker-compose.yml").read_text()


def _service(name: str) -> str:
    return COMPOSE.split(f"\n  {name}:\n", 1)[1].split("\n\n", 1)[0]


def test_core_and_bridge_use_companion_redis_with_the_password() -> None:
    urls = re.findall(r'REDIS_URL: "([^"]+)"', COMPOSE)
    assert len(urls) == 2
    for url in urls:
        assert url.startswith("redis://:${KLUKAI_REDIS_PASSWORD:?")
        assert url.endswith("@companion-redis:6379/0")


def test_password_url_parses_without_connecting() -> None:
    client = redis.from_url("redis://:0123abcd@companion-redis:6379/0")
    kwargs = client.connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == (
        "companion-redis",
        6379,
        0,
    )
    assert kwargs["password"] == "0123abcd"


def test_redis_is_unpublished_capped_and_locked_down() -> None:
    service = _service("redis")
    assert "container_name: companion-redis" in service
    assert "ports:" not in service
    assert "valkey/valkey@sha256:" in service
    assert "read_only: true" in service and "cap_drop: [ALL]" in service
    for line in (
        "requirepass $$KLUKAI_REDIS_PASSWORD",
        "maxmemory 200mb",
        "maxmemory-policy volatile-lru",
        "appendonly yes",
    ):
        assert line in service
    for command in ("FLUSHALL", "FLUSHDB", "CONFIG", "DEBUG"):
        assert f'rename-command {command} \\"\\"' in service


def test_core_and_bridge_wait_for_a_healthy_redis() -> None:
    for name in ("companion-core", "events-bridge"):
        assert (
            "depends_on:\n      redis:\n        condition: service_healthy"
            in _service(name)
        )
