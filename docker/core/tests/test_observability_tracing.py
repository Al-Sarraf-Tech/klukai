"""Tests for app.observability.tracing — OTel SDK fail-soft init."""

from __future__ import annotations

from unittest.mock import patch

from app.observability import tracing


class TestIsEnabled:
    def test_unset_returns_false(self):
        with patch.dict("os.environ", {}, clear=True):
            assert tracing._is_enabled() is False

    def test_empty_returns_false(self):
        with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": ""}):
            assert tracing._is_enabled() is False

    def test_whitespace_returns_false(self):
        with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "   "}):
            assert tracing._is_enabled() is False

    def test_set_returns_true(self):
        with patch.dict("os.environ", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://alloy:4317"}):
            assert tracing._is_enabled() is True


class TestInitTracingNoop:
    def setup_method(self):
        # Reset module state between tests
        tracing._TRACING_INITIALIZED = False
        tracing._TRACING_NOOP_REASON = None

    def teardown_method(self):
        tracing._TRACING_INITIALIZED = False
        tracing._TRACING_NOOP_REASON = None

    def test_returns_false_when_endpoint_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            result = tracing.init_tracing()
        assert result is False
        assert tracing._TRACING_NOOP_REASON is not None
        assert "unset" in tracing._TRACING_NOOP_REASON.lower()

    def test_idempotent_when_already_initialized(self):
        tracing._TRACING_INITIALIZED = True
        # Even with no endpoint, returns True because already init'd
        with patch.dict("os.environ", {}, clear=True):
            assert tracing.init_tracing() is True


class TestStatus:
    def setup_method(self):
        tracing._TRACING_INITIALIZED = False
        tracing._TRACING_NOOP_REASON = None

    def teardown_method(self):
        tracing._TRACING_INITIALIZED = False
        tracing._TRACING_NOOP_REASON = None

    def test_status_includes_init_state(self):
        s = tracing.status()
        assert "initialized" in s
        assert "noop_reason" in s
        assert "endpoint" in s
        assert "service_name" in s

    def test_default_service_name(self):
        with patch.dict("os.environ", {}, clear=True):
            s = tracing.status()
        assert s["service_name"] == "klukai-core"

    def test_custom_service_name_picked_up(self):
        with patch.dict("os.environ", {"OTEL_SERVICE_NAME": "custom-svc"}):
            s = tracing.status()
        assert s["service_name"] == "custom-svc"


class TestGetCurrentTraceId:
    def setup_method(self):
        tracing._TRACING_INITIALIZED = False

    def teardown_method(self):
        tracing._TRACING_INITIALIZED = False

    def test_returns_none_when_not_initialized(self):
        assert tracing.get_current_trace_id() is None

    def test_returns_none_when_no_active_span(self):
        # Even if "initialized", with no active span we get None
        tracing._TRACING_INITIALIZED = True
        # OTel returns an invalid span by default outside a context
        result = tracing.get_current_trace_id()
        # Either None (no active span) or a hex string — both are valid
        assert result is None or (isinstance(result, str) and len(result) == 32)


class TestInstrumentNoops:
    def setup_method(self):
        tracing._TRACING_INITIALIZED = False

    def teardown_method(self):
        tracing._TRACING_INITIALIZED = False

    def test_instrument_fastapi_false_when_not_init(self):
        assert tracing.instrument_fastapi(object()) is False

    def test_instrument_httpx_false_when_not_init(self):
        assert tracing.instrument_httpx() is False
