"""Unit tests for app.circuit_breakers.

Per S+ Phase 4 (docs/superpowers/specs/2026-05-16-s-plus-uplift.md §5.4)."""

from __future__ import annotations

import asyncio

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


@pytest.mark.asyncio
async def test_force_open_rejects_the_very_next_call() -> None:
    """force_open anchors the probe clock like _trip does; without that the
    next call was promoted straight to half-open and probed the dep the
    chaos drill had just cut off."""
    cb = CircuitBreaker(cfg=BreakerConfig(name="drill", probe_interval_seconds=30))
    calls = 0

    async def ok() -> int:
        nonlocal calls
        calls += 1
        return 1

    cb.force_open()
    with pytest.raises(CircuitOpen):
        await cb.call(ok)
    assert calls == 0
    assert cb.state == State.OPEN


@pytest.mark.asyncio
async def test_half_open_waiter_is_rejected_once_the_probe_settles() -> None:
    """Exactly one probe: a call queued behind the half-open lock finds the
    breaker already closed (or re-tripped) when it gets the lock and is
    rejected instead of running as a second probe."""
    cb = CircuitBreaker(
        cfg=BreakerConfig(name="probe-race", error_threshold=1, window_seconds=10, probe_interval_seconds=0)
    )

    async def boom() -> int:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await cb.call(boom)
    assert cb.state == State.OPEN

    release = asyncio.Event()
    probe_calls = 0

    async def slow_ok() -> str:
        nonlocal probe_calls
        probe_calls += 1
        await release.wait()
        return "ok"

    probe = asyncio.create_task(cb.call(slow_ok))
    await asyncio.sleep(0)  # the probe takes the lock and parks in slow_ok
    assert cb.state == State.HALF_OPEN
    waiter = asyncio.create_task(cb.call(slow_ok))
    await asyncio.sleep(0)  # the waiter parks on the lock
    release.set()

    assert await probe == "ok"
    assert cb.state == State.CLOSED
    with pytest.raises(CircuitOpen):
        await waiter
    assert probe_calls == 1  # the waiter never ran as a second probe


def test_all_breakers_returns_registry() -> None:
    get_breaker("dep-a")
    get_breaker("dep-b")
    snapshot = all_breakers()
    assert "dep-a" in snapshot
    assert "dep-b" in snapshot


@pytest.mark.asyncio
async def test_state_change_log_lines_name_the_dependency(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The core logs plain text, which drops `extra=` fields: production showed
    # a bare "circuit breaker tripped" with no way to tell which dep it was.
    cb = CircuitBreaker(
        cfg=BreakerConfig(
            name="lm_studio", error_threshold=2, window_seconds=10, probe_interval_seconds=0
        )
    )

    async def boom() -> int:
        raise RuntimeError("boom")

    async def ok() -> int:
        return 1

    with caplog.at_level("INFO", logger="app.circuit_breakers"):
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(boom)
        await cb.call(ok)

    messages = [r.getMessage() for r in caplog.records if r.name == "app.circuit_breakers"]
    assert messages == [
        "circuit breaker lm_studio tripped (2 errors in window)",
        "circuit breaker lm_studio probing (half-open)",
        "circuit breaker lm_studio closed",
    ]
