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
_gauges: dict[str, float] = {}

# Latency histograms: bucket lower-bound (ms) -> count
_LATENCY_BUCKETS_MS = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
_histograms: dict[str, dict[float, int]] = defaultdict(
    lambda: {b: 0 for b in _LATENCY_BUCKETS_MS + [float("inf")]}
)
_histogram_totals: dict[str, tuple[int, float]] = defaultdict(
    lambda: (0, 0.0)
)  # (count, sum)

# Process start for uptime reporting
_started_at: float = time.monotonic()


def incr(name: str, by: int = 1, **labels: object) -> None:
    """Increment a counter. Labels form a compound key."""
    key = _label_key(name, labels)
    with _lock:
        _counters[key] += by


def observe_latency(name: str, millis: float, **labels: object) -> None:
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
            buckets[float("inf")] += 1
        count, total = _histogram_totals[key]
        _histogram_totals[key] = (count + 1, total + millis)


def _label_key(name: str, labels: dict[str, object]) -> str:
    if not labels:
        return name
    parts = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{parts}}}"


def set_gauge(name: str, value: float, **labels: object) -> None:
    """Set a current state value under bounded dependency/route labels."""
    with _lock:
        _gauges[_label_key(name, labels)] = value


def snapshot() -> dict[str, Any]:
    """Return a JSON-serializable snapshot of all metrics."""
    with _lock:
        return {
            "uptime_seconds": round(time.monotonic() - _started_at, 1),
            "counters": dict(_counters),
            "gauges": dict(_gauges),
            "histograms": {
                name: {
                    "buckets": {str(k): v for k, v in buckets.items()},
                    "count": _histogram_totals[name][0],
                    "sum_ms": round(_histogram_totals[name][1], 2),
                    "avg_ms": round(
                        _histogram_totals[name][1] / _histogram_totals[name][0], 2
                    )
                    if _histogram_totals[name][0]
                    else 0,
                }
                for name, buckets in _histograms.items()
            },
        }


def reset_for_tests() -> None:
    """Clear all counters and histograms. Test-only."""
    with _lock:
        _counters.clear()
        _gauges.clear()
        _histograms.clear()
        _histogram_totals.clear()


def record_http(route: str, method: str, status: int, elapsed_ms: float) -> None:
    """Bound method/status labels; route must be a registered template."""
    method = (
        method
        if method in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
        else "OTHER"
    )
    status_class = f"{status // 100}xx" if 100 <= status < 600 else "other"
    incr("http_requests_total", route=route, method=method, status_class=status_class)
    observe_latency("http_request_duration_ms", elapsed_ms, route=route, method=method)


def _prom_labels(compound: str, **extra: object) -> tuple[str, str]:
    """Convert the legacy snapshot compound keys to escaped Prom labels."""
    import json

    name, _, labels = compound.partition("{")
    pairs = dict(
        part.split("=", 1)
        for part in labels.removesuffix("}").split(",")
        if "=" in part
    )
    pairs.update({k: str(v) for k, v in extra.items()})
    rendered = ",".join(f"{k}={json.dumps(v)}" for k, v in sorted(pairs.items()))
    return name, "{" + rendered + "}" if pairs else ""


def prometheus_text() -> str:
    """Expose real cumulative histogram buckets for an authenticated scrape."""
    snap = snapshot()
    lines = [f"klukai_uptime_seconds {snap['uptime_seconds']}"]
    for section in ("counters", "gauges"):
        for compound, value in snap[section].items():
            name, labels = _prom_labels(compound)
            lines.append(f"klukai_{name}{labels} {value}")
    for compound, histogram in snap["histograms"].items():
        total = 0
        for bound, count in histogram["buckets"].items():
            total += count
            name, labels = _prom_labels(
                compound, le="+Inf" if bound == "inf" else bound
            )
            lines.append(f"klukai_{name}_bucket{labels} {total}")
        name, labels = _prom_labels(compound)
        lines.append(f"klukai_{name}_count{labels} {histogram['count']}")
        lines.append(f"klukai_{name}_sum{labels} {histogram['sum_ms']}")
    return "\n".join(lines) + "\n"
