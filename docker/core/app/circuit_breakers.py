"""Circuit breakers — graceful degradation per external dependency.

S+ Phase 4 deliverable (per docs/superpowers/specs/2026-05-16-s-plus-uplift.md §5.4).

A circuit breaker is a state machine that protects klukai-core from cascading
failures when an external dependency degrades. Three states:

- **closed** — normal traffic flows. Errors are counted in a sliding window.
- **open** — calls fail-fast without hitting the dep. Periodic half-open probe.
- **half_open** — exactly one probe call allowed; outcome flips state.

The breaker emits Prom metrics + OTel span attributes on every state
transition, so dashboards can show breaker state per dep and alerts can
fire on `open` lasting >5 min.

Per-dep configuration matches §5.4 of the spec:

| Dep | Open threshold | Half-open probe | Fallback strategy |
|---|---|---|---|
| postgres   | 5 errs / 10s  | 1 query / 30s  | Serve cached state from Redis |
| redis      | 5 errs / 10s  | 1 PING / 15s   | Disable session caching; PG passthrough |
| qdrant     | 3 errs / 30s  | 1 search / 60s | Empty episodic memory; PG fallback |
| lm_studio  | 3 errs / 60s  | 1 health / 120s | FAILURE_SENTINEL (local-only, no cloud) |
| voice      | 5 errs / 60s  | 1 health / 120s | Skip TTS; text-only |
| comfyui    | 3 errs / 60s  | 1 health / 300s | Skip image gen; "image unavailable" |

Usage::

    from app.circuit_breakers import get_breaker

    breaker = get_breaker("postgres")
    try:
        result = await breaker.call(some_async_pg_call, arg1, arg2)
    except CircuitOpen:
        # fallback path
        result = fallback_value()
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class State(str, Enum):
    """Circuit-breaker state machine."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(Exception):
    """Raised when a call is rejected because the breaker is open.

    Callers catch this to invoke their fallback path. The exception itself
    is cheap (no traceback walk in the hot path)."""


@dataclass
class BreakerConfig:
    """Per-dep tuning. Defaults err on the safe side."""

    name: str
    error_threshold: int = 5
    window_seconds: float = 10.0
    probe_interval_seconds: float = 30.0
    half_open_max_concurrent: int = 1

    @classmethod
    def for_dep(cls, name: str) -> BreakerConfig:
        """Per-spec configurations from §5.4."""
        presets = {
            "postgres":  cls(name="postgres",  error_threshold=5, window_seconds=10,  probe_interval_seconds=30),
            "redis":     cls(name="redis",     error_threshold=5, window_seconds=10,  probe_interval_seconds=15),
            "qdrant":    cls(name="qdrant",    error_threshold=3, window_seconds=30,  probe_interval_seconds=60),
            "lm_studio": cls(name="lm_studio", error_threshold=3, window_seconds=60,  probe_interval_seconds=120),
            "voice":     cls(name="voice",     error_threshold=5, window_seconds=60,  probe_interval_seconds=120),
            "comfyui":   cls(name="comfyui",   error_threshold=3, window_seconds=60,  probe_interval_seconds=300),
        }
        return presets.get(name, cls(name=name))


@dataclass
class CircuitBreaker:
    """Async circuit breaker for a single dependency.

    Thread-unsafe but asyncio-safe. Use `get_breaker(name)` to fetch
    a singleton per dep — that's the supported access pattern.
    """

    cfg: BreakerConfig
    _state: State = State.CLOSED
    _errors: deque[float] = field(default_factory=lambda: deque(maxlen=64))
    _last_probe: float = 0.0
    _half_open_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def state(self) -> State:
        return self._state

    def _prune_window(self, now: float) -> None:
        cutoff = now - self.cfg.window_seconds
        while self._errors and self._errors[0] < cutoff:
            self._errors.popleft()

    def _trip(self) -> None:
        if self._state != State.OPEN:
            logger.warning(
                "circuit breaker tripped",
                extra={"dep": self.cfg.name, "errors_in_window": len(self._errors)},
            )
            self._state = State.OPEN
            # Anchor the probe-interval clock to trip time so the next call
            # immediately after a trip is rejected (not promoted to half-open).
            self._last_probe = time.time()
            _record_state_transition(self.cfg.name, State.OPEN)

    def _close(self) -> None:
        if self._state != State.CLOSED:
            logger.info("circuit breaker closed", extra={"dep": self.cfg.name})
            self._state = State.CLOSED
            self._errors.clear()
            _record_state_transition(self.cfg.name, State.CLOSED)

    def _maybe_half_open(self, now: float) -> None:
        if self._state == State.OPEN and (now - self._last_probe) >= self.cfg.probe_interval_seconds:
            logger.info("circuit breaker probing (half-open)", extra={"dep": self.cfg.name})
            self._state = State.HALF_OPEN
            _record_state_transition(self.cfg.name, State.HALF_OPEN)

    async def call(self, fn: Callable[..., Awaitable[T]], *args: object, **kwargs: object) -> T:
        """Invoke `fn` under breaker protection. Raises CircuitOpen if rejected."""
        now = time.time()
        self._prune_window(now)
        self._maybe_half_open(now)

        if self._state == State.OPEN:
            raise CircuitOpen(f"breaker open for {self.cfg.name!r}")

        if self._state == State.HALF_OPEN:
            # Serialize the probe — only one concurrent call allowed.
            async with self._half_open_lock:
                self._last_probe = now
                try:
                    result = await fn(*args, **kwargs)
                except Exception:
                    self._errors.append(now)
                    self._trip()
                    raise
                self._close()
                return result

        # CLOSED — normal path.
        try:
            return await fn(*args, **kwargs)
        except Exception:
            self._errors.append(now)
            self._prune_window(now)
            if len(self._errors) >= self.cfg.error_threshold:
                self._trip()
            raise

    def force_open(self) -> None:
        """Trip the breaker. Used by chaos drills + manual ops."""
        self._state = State.OPEN
        _record_state_transition(self.cfg.name, State.OPEN)

    def force_close(self) -> None:
        """Force close. Used by manual ops after dep recovery confirmed."""
        self._close()


# ── Registry — one breaker per dep ───────────────────────────────────────────
_BREAKERS: dict[str, CircuitBreaker] = {}


def get_breaker(name: str) -> CircuitBreaker:
    """Return (and lazily create) the singleton breaker for `name`."""
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(cfg=BreakerConfig.for_dep(name))
    return _BREAKERS[name]


def all_breakers() -> dict[str, CircuitBreaker]:
    """Return all known breakers (for /health and metrics)."""
    return dict(_BREAKERS)


# ── Metrics emission (no hard dep on Prom client — fails open) ───────────────
def _record_state_transition(dep: str, new_state: State) -> None:
    """Emit a Prom metric on every state change. Fail-open if metrics not wired."""
    try:
        from app.observability.metrics import circuit_state_gauge

        # Encode: closed=0, half_open=1, open=2.
        mapping = {State.CLOSED: 0, State.HALF_OPEN: 1, State.OPEN: 2}
        circuit_state_gauge.labels(dep=dep).set(mapping[new_state])
    except Exception:
        # Metrics not initialized in this context — that's fine.
        pass

    # Emit OTel span attribute if a span is active.
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span and span.is_recording():
            span.set_attribute(f"klukai.circuit.{dep}.state", new_state.value)
    except Exception:
        pass


__all__ = [
    "BreakerConfig",
    "CircuitBreaker",
    "CircuitOpen",
    "State",
    "all_breakers",
    "get_breaker",
]
