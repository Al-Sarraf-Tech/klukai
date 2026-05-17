"""OpenTelemetry SDK initialization for companion-core.

Per S+ uplift spec §5.2: companion-core emits OTLP traces + metrics
to alloy:4317 (the obs stack scaffolded by docker-compose.obs.yml).

This module is **fail-soft**:
- If `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, all init becomes no-ops
  (companion-core runs unchanged — useful for dev / when the obs
  stack isn't deployed).
- If OTel imports fail, log a warning and continue. Klukai never goes
  down because of an observability misconfiguration.
- If the OTLP endpoint is unreachable at runtime, the SDK queues
  spans then drops them silently. No backpressure on the request path.

Usage from app/main.py:

    from .observability.tracing import init_tracing, instrument_fastapi
    init_tracing()
    instrument_fastapi(app)

Env vars consumed:
- `OTEL_EXPORTER_OTLP_ENDPOINT` — alloy gRPC endpoint, e.g. http://alloy:4317
- `OTEL_SERVICE_NAME` — defaults to "klukai-core"
- `OTEL_RESOURCE_ATTRIBUTES` — extra k=v,k=v attrs (standard OTel env)
- `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` — standard OTel
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Track init state for tests + observability of observability
_TRACING_INITIALIZED = False
_TRACING_NOOP_REASON: str | None = None


def _is_enabled() -> bool:
    """Tracing is enabled iff OTEL_EXPORTER_OTLP_ENDPOINT is non-empty."""
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


def init_tracing() -> bool:
    """Initialize the OTel SDK. Returns True on success, False on no-op or failure.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _TRACING_INITIALIZED, _TRACING_NOOP_REASON

    if _TRACING_INITIALIZED:
        return True

    if not _is_enabled():
        _TRACING_NOOP_REASON = "OTEL_EXPORTER_OTLP_ENDPOINT unset"
        logger.info("OTel tracing: %s (no-op)", _TRACING_NOOP_REASON)
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        service_name = os.getenv("OTEL_SERVICE_NAME", "klukai-core")
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()

        resource = Resource.create({
            "service.name": service_name,
            "service.namespace": "klukai",
            "deployment.environment": os.getenv("KLUKAI_ENV", "production"),
        })

        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        _TRACING_INITIALIZED = True
        _TRACING_NOOP_REASON = None
        logger.info(
            "OTel tracing initialized: service=%s endpoint=%s",
            service_name, endpoint,
        )
        return True

    except ImportError as e:
        _TRACING_NOOP_REASON = f"OTel SDK not installed: {e}"
        logger.warning("OTel tracing: %s (no-op)", _TRACING_NOOP_REASON)
        return False
    except Exception as e:
        _TRACING_NOOP_REASON = f"OTel init failed: {e}"
        logger.warning("OTel tracing: %s (no-op)", _TRACING_NOOP_REASON)
        return False


def instrument_fastapi(app: Any) -> bool:
    """Wrap a FastAPI app with OTel HTTP middleware. Fail-soft."""
    if not _TRACING_INITIALIZED:
        return False
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        logger.info("OTel FastAPI instrumentation enabled")
        return True
    except Exception as e:
        logger.warning("OTel FastAPI instrumentation failed: %s", e)
        return False


def instrument_httpx() -> bool:
    """Add OTel auto-instrumentation to httpx clients (outbound calls)."""
    if not _TRACING_INITIALIZED:
        return False
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        logger.info("OTel httpx instrumentation enabled")
        return True
    except Exception as e:
        logger.warning("OTel httpx instrumentation failed: %s", e)
        return False


def get_current_trace_id() -> str | None:
    """Return the current span's trace_id as a hex string, or None.

    Used by structured_log to add trace_id to every log line so
    Loki↔Tempo correlation works in Grafana.
    """
    if not _TRACING_INITIALIZED:
        return None
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx.is_valid and ctx.trace_id != 0:
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    return None


def status() -> dict[str, Any]:
    """Return current tracing state — used by /api/health/subsystems."""
    return {
        "initialized": _TRACING_INITIALIZED,
        "noop_reason": _TRACING_NOOP_REASON,
        "endpoint": os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip() or None,
        "service_name": os.getenv("OTEL_SERVICE_NAME", "klukai-core"),
    }
