"""Unit tests for app.circuit_breakers.

Per S+ Phase 4 (docs/superpowers/specs/2026-05-16-s-plus-uplift.md §5.4)."""

from __future__ import annotations


import pytest

from app.circuit_breakers import (
    BreakerConfig,
    CircuitBreaker,
    CircuitOpen,
    State,
    all_breakers,
    get_breaker,
)


@pytest.mark.asyncio
async def test_breaker_starts_closed() -> None:
    cb = CircuitBreaker(cfg=BreakerConfig(name="test1", error_threshold=3, window_seconds=10))
    assert cb.state == State.CLOSED


@pytest.mark.asyncio
async def test_breaker_passes_calls_when_closed() -> None:
    cb = CircuitBreaker(cfg=BreakerConfig(name="test2", error_threshold=3, window_seconds=10))

    async def ok() -> int:
        return 42

    assert await cb.call(ok) == 42
    assert cb.state == State.CLOSED


@pytest.mark.asyncio
async def test_breaker_trips_after_threshold() -> None:
    cb = CircuitBreaker(cfg=BreakerConfig(name="test3", error_threshold=3, window_seconds=10))

    async def boom() -> int:
        raise RuntimeError("boom")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call(boom)
    assert cb.state == State.OPEN

    # Next call rejected without invoking fn.
    with pytest.raises(CircuitOpen):
        await cb.call(boom)


@pytest.mark.asyncio
async def test_breaker_half_open_recovers_on_success() -> None:
    cb = CircuitBreaker(
        cfg=BreakerConfig(name="test4", error_threshold=2, window_seconds=10, probe_interval_seconds=0)
    )

    async def boom() -> int:
        raise RuntimeError("boom")

    async def ok() -> int:
        return 7

    # Trip the breaker.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(boom)
    assert cb.state == State.OPEN

    # probe_interval_seconds=0 → immediately eligible for half-open probe.
    result = await cb.call(ok)
    assert result == 7
    assert cb.state == State.CLOSED


@pytest.mark.asyncio
async def test_breaker_half_open_failure_retrips() -> None:
    cb = CircuitBreaker(
        cfg=BreakerConfig(name="test5", error_threshold=2, window_seconds=10, probe_interval_seconds=0)
    )

    async def boom() -> int:
        raise RuntimeError("boom")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call(boom)
    assert cb.state == State.OPEN

    # Half-open probe also fails → straight back to open.
    with pytest.raises(RuntimeError):
        await cb.call(boom)
    assert cb.state == State.OPEN


def test_get_breaker_singleton() -> None:
    a = get_breaker("singleton-dep")
    b = get_breaker("singleton-dep")
    assert a is b


def test_for_dep_presets_match_spec() -> None:
    """Per spec §5.4: postgres / qdrant / lm_studio / voice / comfyui."""
    for name in ("postgres", "redis", "qdrant", "lm_studio", "voice", "comfyui"):
        cfg = BreakerConfig.for_dep(name)
        assert cfg.name == name
        assert cfg.error_threshold > 0
        assert cfg.window_seconds > 0


def test_force_open_close() -> None:
    cb = CircuitBreaker(cfg=BreakerConfig(name="forceable"))
    cb.force_open()
    assert cb.state == State.OPEN
    cb.force_close()
    assert cb.state == State.CLOSED


def test_all_breakers_returns_registry() -> None:
    get_breaker("dep-a")
    get_breaker("dep-b")
    snapshot = all_breakers()
    assert "dep-a" in snapshot
    assert "dep-b" in snapshot
