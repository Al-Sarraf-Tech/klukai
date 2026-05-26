"""Behavioral coverage for app.observability.tracing — remaining OTel paths.

tests/test_observability_tracing.py already covers _is_enabled, the no-op /
idempotent init guards, status() shape, the not-initialized instrument guards,
and get_current_trace_id when uninitialized. This file drives the REMAINING
lines: the full SDK init SUCCESS path, the ImportError + generic-Exception
fail-soft branches, instrument_fastapi/httpx success + failure, status()
endpoint reporting, and get_current_trace_id with a valid span.

The OTel SDK is installed in the venv, but we mock the SDK objects so we never
mutate the process-global TracerProvider (which would leak into the rest of the
suite). Every test resets the module's init globals in setup + teardown.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.observability import tracing


def _reset():
    tracing._TRACING_INITIALIZED = False
    tracing._TRACING_NOOP_REASON = None


@pytest.fixture(autouse=True)
def _reset_state():
    _reset()
    yield
    _reset()


# ── init_tracing success path ────────────────────────────────────────────────

class TestInitTracingSuccess:
    def test_full_init_wires_provider_and_exporter(self):
        """Endpoint set + SDK importable → provider built, exporter attached,
        set_tracer_provider called, returns True."""
        provider = MagicMock()
        provider_cls = MagicMock(return_value=provider)
        exporter = MagicMock()
        exporter_cls = MagicMock(return_value=exporter)
        processor = MagicMock()
        processor_cls = MagicMock(return_value=processor)
        resource = MagicMock()
        set_provider = MagicMock()

        env = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://alloy:4317",
            "OTEL_SERVICE_NAME": "klukai-core-test",
            "KLUKAI_ENV": "staging",
        }
        with patch.dict("os.environ", env), \
             patch("opentelemetry.trace.set_tracer_provider", set_provider), \
             patch("opentelemetry.sdk.trace.TracerProvider", provider_cls), \
             patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter",
                   exporter_cls), \
             patch("opentelemetry.sdk.trace.export.BatchSpanProcessor", processor_cls), \
             patch("opentelemetry.sdk.resources.Resource.create", return_value=resource):
            result = tracing.init_tracing()

        assert result is True
        assert tracing._TRACING_INITIALIZED is True
        assert tracing._TRACING_NOOP_REASON is None
        # Exporter built against the configured endpoint, insecure gRPC.
        exporter_cls.assert_called_once_with(endpoint="http://alloy:4317", insecure=True)
        # Provider got the batch processor and was registered globally.
        provider.add_span_processor.assert_called_once_with(processor)
        set_provider.assert_called_once_with(provider)

    def test_second_call_is_idempotent_noop(self):
        """Once initialized, a repeat call returns True without re-importing."""
        tracing._TRACING_INITIALIZED = True
        # If it tried the SDK path it'd touch os.environ; assert it doesn't even check.
        with patch.object(tracing, "_is_enabled") as enabled:
            assert tracing.init_tracing() is True
        enabled.assert_not_called()

    def test_import_error_is_fail_soft(self):
        """A missing OTel SDK → returns False, records noop reason, no raise."""
        with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://alloy:4317"}), \
             patch("opentelemetry.sdk.trace.TracerProvider",
                   side_effect=ImportError("no sdk")):
            result = tracing.init_tracing()
        assert result is False
        assert tracing._TRACING_INITIALIZED is False
        assert "not installed" in tracing._TRACING_NOOP_REASON

    def test_generic_exception_is_fail_soft(self):
        """Any other init failure is caught — Klukai never dies on obs config."""
        with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://alloy:4317"}), \
             patch("opentelemetry.sdk.trace.TracerProvider",
                   side_effect=RuntimeError("boom")):
            result = tracing.init_tracing()
        assert result is False
        assert tracing._TRACING_INITIALIZED is False
        assert "init failed" in tracing._TRACING_NOOP_REASON


# ── instrument_fastapi ───────────────────────────────────────────────────────

class TestInstrumentFastapi:
    def test_success_when_initialized(self):
        tracing._TRACING_INITIALIZED = True
        instrumentor = MagicMock()
        app = object()
        with patch("opentelemetry.instrumentation.fastapi.FastAPIInstrumentor",
                   instrumentor):
            assert tracing.instrument_fastapi(app) is True
        instrumentor.instrument_app.assert_called_once_with(app)

    def test_failure_is_fail_soft(self):
        tracing._TRACING_INITIALIZED = True
        instrumentor = MagicMock()
        instrumentor.instrument_app.side_effect = RuntimeError("instr fail")
        with patch("opentelemetry.instrumentation.fastapi.FastAPIInstrumentor",
                   instrumentor):
            assert tracing.instrument_fastapi(object()) is False


# ── instrument_httpx ─────────────────────────────────────────────────────────

class TestInstrumentHttpx:
    def test_success_when_initialized(self):
        tracing._TRACING_INITIALIZED = True
        inst_instance = MagicMock()
        inst_cls = MagicMock(return_value=inst_instance)
        with patch("opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor",
                   inst_cls):
            assert tracing.instrument_httpx() is True
        inst_instance.instrument.assert_called_once()

    def test_failure_is_fail_soft(self):
        tracing._TRACING_INITIALIZED = True
        inst_instance = MagicMock()
        inst_instance.instrument.side_effect = RuntimeError("httpx instr fail")
        inst_cls = MagicMock(return_value=inst_instance)
        with patch("opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor",
                   inst_cls):
            assert tracing.instrument_httpx() is False


# ── get_current_trace_id with a valid span ───────────────────────────────────

class TestGetCurrentTraceId:
    def test_returns_hex_when_span_valid(self):
        tracing._TRACING_INITIALIZED = True
        ctx = MagicMock()
        ctx.is_valid = True
        ctx.trace_id = 0x0123456789ABCDEF0123456789ABCDEF
        span = MagicMock()
        span.get_span_context.return_value = ctx
        with patch("opentelemetry.trace.get_current_span", return_value=span):
            tid = tracing.get_current_trace_id()
        assert tid == "0123456789abcdef0123456789abcdef"
        assert len(tid) == 32

    def test_returns_none_when_trace_id_zero(self):
        tracing._TRACING_INITIALIZED = True
        ctx = MagicMock()
        ctx.is_valid = True
        ctx.trace_id = 0
        span = MagicMock()
        span.get_span_context.return_value = ctx
        with patch("opentelemetry.trace.get_current_span", return_value=span):
            assert tracing.get_current_trace_id() is None

    def test_returns_none_on_exception(self):
        tracing._TRACING_INITIALIZED = True
        with patch("opentelemetry.trace.get_current_span",
                   side_effect=RuntimeError("no ctx")):
            assert tracing.get_current_trace_id() is None


# ── status endpoint reporting ────────────────────────────────────────────────

class TestStatusEndpoint:
    def test_endpoint_reported_when_set(self):
        with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://alloy:4317"}):
            s = tracing.status()
        assert s["endpoint"] == "http://alloy:4317"
        assert s["initialized"] is False

    def test_endpoint_none_when_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            s = tracing.status()
        assert s["endpoint"] is None

    def test_reflects_initialized_flag(self):
        tracing._TRACING_INITIALIZED = True
        tracing._TRACING_NOOP_REASON = None
        s = tracing.status()
        assert s["initialized"] is True
        assert s["noop_reason"] is None


# ── fail-soft guards when disabled / not initialized ─────────────────────────
# These complete the branch coverage for the no-op paths and assert the exact
# fail-soft contract (return value + recorded reason), not just truthiness.

class TestDisabledNoops:
    def test_init_noop_when_endpoint_unset_records_reason(self):
        with patch.dict("os.environ", {}, clear=True):
            assert tracing.init_tracing() is False
        assert tracing._TRACING_INITIALIZED is False
        assert "unset" in tracing._TRACING_NOOP_REASON.lower()

    def test_instrument_fastapi_returns_false_when_not_initialized(self):
        # _TRACING_INITIALIZED is False (reset fixture) → guard short-circuits.
        sentinel_app = object()
        assert tracing.instrument_fastapi(sentinel_app) is False

    def test_instrument_httpx_returns_false_when_not_initialized(self):
        assert tracing.instrument_httpx() is False

    def test_trace_id_none_when_not_initialized(self):
        assert tracing.get_current_trace_id() is None
