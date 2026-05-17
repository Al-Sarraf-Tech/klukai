"""In-process metrics counters for /api/metrics endpoint.

Counter + histogram primitives without a Prometheus dependency. Exposes
a snapshot() dict which the metrics route serializes as JSON or
Prometheus-text on request.
"""

from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict
from typing import Any


_lock = threading.Lock()

# Monotonic counters
_counters: Counter[str] = Counter()

# Latency histograms: bucket lower-bound (ms) -> count
_LATENCY_BUCKETS_MS = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
_histograms: dict[str, dict[int, int]] = defaultdict(lambda: {b: 0 for b in _LATENCY_BUCKETS_MS + [float("inf")]})
_histogram_totals: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))  # (count, sum)

# Process start for uptime reporting
_started_at: float = time.monotonic()


def incr(name: str, by: int = 1, **labels: Any) -> None:
    """Increment a counter. Labels form a compound key."""
    key = _label_key(name, labels)
    with _lock:
        _counters[key] += by


def observe_latency(name: str, millis: float, **labels: Any) -> None:
    """Record a latency measurement into a bucketed histogram."""
    key = _label_key(name, labels)
    with _lock:
        buckets = _histograms[key]
        placed = False
        for b in _LATENCY_BUCKETS_MS:
            if millis <= b:
                buckets[b] += 1
                placed = True
                break
        if not placed:
            buckets[float("inf")] += 1  # type: ignore[index]
        count, total = _histogram_totals[key]
        _histogram_totals[key] = (count + 1, total + millis)


def _label_key(name: str, labels: dict) -> str:
    if not labels:
        return name
    parts = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{parts}}}"


def snapshot() -> dict[str, Any]:
    """Return a JSON-serializable snapshot of all metrics."""
    with _lock:
        return {
            "uptime_seconds": round(time.monotonic() - _started_at, 1),
            "counters": dict(_counters),
            "histograms": {
                name: {
                    "buckets": {str(k): v for k, v in buckets.items()},
                    "count": _histogram_totals[name][0],
                    "sum_ms": round(_histogram_totals[name][1], 2),
                    "avg_ms": round(
                        _histogram_totals[name][1] / _histogram_totals[name][0], 2
                    ) if _histogram_totals[name][0] else 0,
                }
                for name, buckets in _histograms.items()
            },
        }


def reset_for_tests() -> None:
    """Clear all counters and histograms. Test-only."""
    with _lock:
        _counters.clear()
        _histograms.clear()
        _histogram_totals.clear()
