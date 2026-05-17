"""Observability primitives for klukai.

Submodules:
- :mod:`.health_cache` — TTL-cached subsystem pings (PG / Redis / Qdrant)

Module-level helpers (re-exported for backward-compatibility):
- :func:`structured_log` — single-line JSON log for indexable observability
- :func:`slow_query_timer` — warn when a wrapped block exceeds threshold_ms
- :func:`record_llm_usage` — push LLM token + latency counters to metrics
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Any


def structured_log(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    """Emit a single-line JSON log for indexable structured observability.

    event  — short machine-readable name (e.g. 'chat.turn', 'llm.complete')
    fields — arbitrary key/value pairs; must be JSON-serializable

    Falls back to plain logger.log if JSON serialization fails, so a bad
    field never drops the log line.
    """
    try:
        payload = json.dumps({"event": event, **fields}, default=str)
        logger.log(level, payload)
    except Exception:
        logger.log(level, "event=%s fields=%r", event, fields)


@contextmanager
def slow_query_timer(logger: logging.Logger, name: str,
                     threshold_ms: float = 500.0):
    """Log a warning if the wrapped block exceeds threshold_ms.

    Usage:
        with slow_query_timer(logger, "companion_memories.search"):
            rows = await conn.execute(...).fetchall()
    """
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms > threshold_ms:
            structured_log(
                logger, logging.WARNING, "slow_query",
                name=name, elapsed_ms=round(elapsed_ms, 1),
                threshold_ms=threshold_ms,
            )
            # Also bump a metric so /api/metrics can show slow query counts
            try:
                from .. import metrics
                metrics.incr("slow_queries_total", query=name)
                metrics.observe_latency("slow_query_ms", elapsed_ms, query=name)
            except Exception:
                pass


def record_llm_usage(
    model: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: float,
    route: str = "chat",
) -> None:
    """Record LLM token usage + latency in the metrics module.

    Called from the LLM router on every completion. Breakdown by model
    + route shows which endpoint is consuming the most tokens.
    """
    try:
        from .. import metrics
        metrics.incr("llm_requests_total", model=model, route=route)
        metrics.incr("llm_tokens_in_total", model=model, route=route, by=max(0, tokens_in))
        metrics.incr("llm_tokens_out_total", model=model, route=route, by=max(0, tokens_out))
        metrics.observe_latency("llm_latency_ms", latency_ms, model=model, route=route)
    except Exception:
        pass  # metrics is best-effort


__all__ = ["structured_log", "slow_query_timer", "record_llm_usage"]
