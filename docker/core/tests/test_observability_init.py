"""Tests for app.observability.__init__ — slow_query_timer + record_llm_usage."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from app.observability import record_llm_usage, slow_query_timer, structured_log


class TestStructuredLog:
    def test_emits_json_log(self):
        logger = MagicMock(spec=logging.Logger)
        structured_log(logger, logging.INFO, "chat.turn", user_id="alice", turns=5)
        logger.log.assert_called_once()
        call_args = logger.log.call_args.args
        # First arg is level
        assert call_args[0] == logging.INFO
        # Second arg is the JSON payload
        payload = call_args[1]
        assert '"event"' in payload
        assert '"chat.turn"' in payload
        assert '"alice"' in payload

    def test_falls_back_on_serialization_error(self):
        logger = MagicMock(spec=logging.Logger)
        # Pass an un-JSON-serializable object
        class Unserializable:
            def __repr__(self):
                return "<unser>"
        # default=str in the impl converts it — so JSON path still works.
        # To force fallback, patch json.dumps to fail.
        with patch("app.observability.json.dumps", side_effect=RuntimeError("ser fail")):
            structured_log(logger, logging.WARNING, "event", k="v")
        # Fallback path also calls logger.log
        logger.log.assert_called_once()
        # Format used in fallback is "event=%s fields=%r"
        assert logger.log.call_args.args[0] == logging.WARNING

    def test_includes_trace_id_when_otel_active(self):
        logger = MagicMock(spec=logging.Logger)
        with patch("app.observability.tracing.get_current_trace_id", return_value="abc-trace"):
            structured_log(logger, logging.INFO, "event", x=1)
        payload = logger.log.call_args.args[1]
        assert "trace_id" in payload
        assert "abc-trace" in payload

    def test_explicit_trace_id_not_overwritten(self):
        logger = MagicMock(spec=logging.Logger)
        with patch("app.observability.tracing.get_current_trace_id", return_value="auto-trace"):
            structured_log(logger, logging.INFO, "event", trace_id="explicit-trace")
        payload = logger.log.call_args.args[1]
        assert "explicit-trace" in payload
        assert "auto-trace" not in payload

    def test_tracing_module_missing_doesnt_break(self):
        """If observability.tracing fails to import, structured_log still works."""
        logger = MagicMock(spec=logging.Logger)
        with patch("app.observability.tracing.get_current_trace_id",
                   side_effect=ImportError("tracing missing")):
            structured_log(logger, logging.INFO, "event", x=1)
        logger.log.assert_called_once()


class TestSlowQueryTimer:
    def test_no_warning_when_fast(self):
        logger = MagicMock(spec=logging.Logger)
        # Wrap a fast no-op block; threshold 500ms — shouldn't trigger
        with slow_query_timer(logger, "my_query", threshold_ms=500.0):
            pass
        logger.log.assert_not_called()

    def test_warns_when_slow(self):
        logger = MagicMock(spec=logging.Logger)
        # Set threshold to 0ms so any work triggers
        import time as _t
        with slow_query_timer(logger, "my_query", threshold_ms=0.0):
            _t.sleep(0.001)  # 1ms — guaranteed > 0
        # The slow_query log line should have been emitted
        # logger.log called via structured_log
        assert logger.log.called

    def test_warns_on_exception_in_block(self):
        logger = MagicMock(spec=logging.Logger)
        try:
            with slow_query_timer(logger, "boom", threshold_ms=0.0):
                raise ValueError("test")
        except ValueError:
            pass
        # Even on exception, the timer logged its warning (finally block)
        assert logger.log.called


class TestRecordLlmUsage:
    def test_calls_metrics_module(self):
        with patch("app.metrics.incr") as incr, \
             patch("app.metrics.observe_latency") as obs:
            record_llm_usage("dolphin", tokens_in=100, tokens_out=50, latency_ms=420.5)
        # incr called for requests_total + tokens_in + tokens_out
        assert incr.call_count >= 3
        # observe_latency called for the latency metric
        obs.assert_called_once()

    def test_clamps_negative_tokens_to_zero(self):
        with patch("app.metrics.incr") as incr, \
             patch("app.metrics.observe_latency"):
            record_llm_usage("dolphin", tokens_in=-5, tokens_out=-10, latency_ms=100)
        # The incr calls for tokens should pass 0, not negative
        for call in incr.call_args_list:
            kwargs = call.kwargs
            if "by" in kwargs:
                assert kwargs["by"] >= 0

    def test_swallows_metrics_failure(self):
        with patch("app.metrics.incr", side_effect=RuntimeError("metrics down")):
            # Should not raise
            record_llm_usage("dolphin", tokens_in=100, tokens_out=50, latency_ms=100)

    def test_custom_route_threaded(self):
        with patch("app.metrics.incr") as incr, \
             patch("app.metrics.observe_latency"):
            record_llm_usage("dolphin", 10, 5, 100, route="image_gen")
        # Every incr call should have route=image_gen
        for call in incr.call_args_list:
            kwargs = call.kwargs
            assert kwargs.get("route") == "image_gen"
