"""Bounded asyncio circuit breakers at external dependency boundaries.

A rejected call never retries or chooses a provider. Callers retain their
existing fail-closed authentication and optional-provider degradation policy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import ParamSpec, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")
P = ParamSpec("P")


class State(str, Enum):
    """Circuit-breaker state machine."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(httpx.TransportError):
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
            "postgres": cls(
                name="postgres",
                error_threshold=5,
                window_seconds=10,
                probe_interval_seconds=30,
            ),
            "redis": cls(
                name="redis",
                error_threshold=5,
                window_seconds=10,
                probe_interval_seconds=15,
            ),
            "qdrant": cls(
                name="qdrant",
                error_threshold=3,
                window_seconds=30,
                probe_interval_seconds=60,
            ),
            "lm_studio": cls(
                name="lm_studio",
                error_threshold=3,
                window_seconds=60,
                probe_interval_seconds=120,
            ),
            "voice": cls(
                name="voice",
                error_threshold=5,
                window_seconds=60,
                probe_interval_seconds=120,
            ),
            "comfyui": cls(
                name="comfyui",
                error_threshold=3,
                window_seconds=60,
                probe_interval_seconds=300,
            ),
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
    _epoch: int = 0
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
            self._epoch += 1
            # Anchor the probe-interval clock to trip time so the next call
            # immediately after a trip is rejected (not promoted to half-open).
            self._last_probe = time.monotonic()
            _record_state_transition(self.cfg.name, State.OPEN)

    def _close(self) -> None:
        if self._state != State.CLOSED:
            logger.info("circuit breaker closed", extra={"dep": self.cfg.name})
            self._state = State.CLOSED
            self._epoch += 1
            self._errors.clear()
            _record_state_transition(self.cfg.name, State.CLOSED)

    def _maybe_half_open(self, now: float) -> None:
        if (
            self._state == State.OPEN
            and (now - self._last_probe) >= self.cfg.probe_interval_seconds
        ):
            logger.info(
                "circuit breaker probing (half-open)", extra={"dep": self.cfg.name}
            )
            self._state = State.HALF_OPEN
            self._epoch += 1
            _record_state_transition(self.cfg.name, State.HALF_OPEN)

    @asynccontextmanager
    async def protect(
        self, *, failures: tuple[type[Exception], ...] = (Exception,)
    ) -> AsyncIterator[None]:
        """Protect one operation, including streaming body consumption.

        Admission is immediate: concurrent half-open callers never queue behind
        a probe. Cancellation reopens a probe and releases admission.
        """
        now = time.monotonic()
        self._prune_window(now)
        self._maybe_half_open(now)
        probe = self._state == State.HALF_OPEN
        if self._state == State.OPEN or (probe and self._half_open_lock.locked()):
            from . import metrics

            metrics.incr("circuit_rejected_total", dep=self.cfg.name)
            raise CircuitOpen(f"breaker open for {self.cfg.name!r}")
        if probe:
            await self._half_open_lock.acquire()
        epoch = self._epoch
        try:
            yield
        except asyncio.CancelledError:
            if probe and self._epoch == epoch:
                self._trip()
            raise
        except Exception as error:
            if isinstance(error, failures):
                self._errors.append(time.monotonic())
                self._prune_window(time.monotonic())
                if probe or len(self._errors) >= self.cfg.error_threshold:
                    self._trip()
            elif probe and self._epoch == epoch:
                self._close()
            raise
        else:
            if probe and self._epoch == epoch:
                self._close()
        finally:
            if probe:
                self._half_open_lock.release()

    async def call(
        self, fn: Callable[P, Awaitable[T]], *args: P.args, **kwargs: P.kwargs
    ) -> T:
        """Invoke a typed operation without retries or provider substitution."""
        async with self.protect():
            return await fn(*args, **kwargs)

    def force_open(self) -> None:
        """Trip the breaker. Used by chaos drills + manual ops."""
        self._state = State.OPEN
        self._epoch += 1
        # Same anchoring as _trip(): without it the very next call promotes
        # the breaker to half-open and probes the dep the drill just cut off.
        self._last_probe = time.monotonic()
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
        from . import metrics

        metrics.set_gauge("circuit_state", 0, dep=name)
    return _BREAKERS[name]


def all_breakers() -> dict[str, CircuitBreaker]:
    """Return all known breakers (for /health and metrics)."""
    return dict(_BREAKERS)


def _record_state_transition(dep: str, new_state: State) -> None:
    """Emit into the same in-process registry exposed by /api/metrics."""
    from . import metrics

    mapping = {State.CLOSED: 0, State.HALF_OPEN: 1, State.OPEN: 2}
    metrics.set_gauge("circuit_state", mapping[new_state], dep=dep)
    metrics.incr("circuit_transitions_total", dep=dep, state=new_state.value)


__all__ = [
    "BreakerConfig",
    "CircuitBreaker",
    "CircuitOpen",
    "State",
    "all_breakers",
    "get_breaker",
]
