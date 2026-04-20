"""Tests for app/observability.py."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestStructuredLog:
    def test_emits_json_with_event_and_fields(self, caplog):
        from app.observability import structured_log
        logger = logging.getLogger("test.structured")
        with caplog.at_level(logging.INFO, logger="test.structured"):
            structured_log(logger, logging.INFO, "user.login",
                           user_id="alice", ip="1.2.3.4")

        assert len(caplog.records) == 1
        payload = json.loads(caplog.records[0].getMessage())
        assert payload["event"] == "user.login"
        assert payload["user_id"] == "alice"
        assert payload["ip"] == "1.2.3.4"

    def test_falls_back_on_non_json_values(self, caplog):
        """Non-serializable objects should fall back to plain repr, not crash."""
        from app.observability import structured_log
        logger = logging.getLogger("test.fallback")

        class Weird:
            __slots__ = ("x",)
            def __init__(self): self.x = 1

        with caplog.at_level(logging.INFO, logger="test.fallback"):
            # default=str handles most things; genuinely unserializable falls back
            structured_log(logger, logging.INFO, "weird.event", obj=Weird())

        assert len(caplog.records) == 1

    def test_respects_level(self, caplog):
        from app.observability import structured_log
        logger = logging.getLogger("test.level")
        with caplog.at_level(logging.WARNING, logger="test.level"):
            structured_log(logger, logging.INFO, "too.quiet")
            structured_log(logger, logging.WARNING, "loud.enough")

        # INFO filtered out, WARNING kept
        assert len(caplog.records) == 1
        payload = json.loads(caplog.records[0].getMessage())
        assert payload["event"] == "loud.enough"


class TestSlowQueryTimer:
    def test_fast_query_no_warning(self, caplog):
        from app.observability import slow_query_timer
        logger = logging.getLogger("test.fast")
        with caplog.at_level(logging.WARNING, logger="test.fast"):
            with slow_query_timer(logger, "fast.op", threshold_ms=1000):
                pass  # instantly fast
        # No slow-query log record
        assert len(caplog.records) == 0

    def test_slow_query_logs_warning_and_bumps_metric(self, caplog):
        from app import metrics
        metrics.reset_for_tests()
        from app.observability import slow_query_timer

        logger = logging.getLogger("test.slow")
        with caplog.at_level(logging.WARNING, logger="test.slow"):
            with slow_query_timer(logger, "slow.op", threshold_ms=5):
                time.sleep(0.02)  # 20ms > 5ms

        assert any("slow_query" in r.getMessage() for r in caplog.records)
        snap = metrics.snapshot()
        assert any("slow_queries_total" in k for k in snap["counters"])
        assert any("slow_query_ms" in k for k in snap["histograms"])

    def test_slow_timer_still_fires_on_exception(self, caplog):
        """Timer is a try/finally so it logs even if the block raises."""
        from app.observability import slow_query_timer
        logger = logging.getLogger("test.err")
        with caplog.at_level(logging.WARNING, logger="test.err"), \
             pytest.raises(RuntimeError):
            with slow_query_timer(logger, "err.op", threshold_ms=5):
                time.sleep(0.02)
                raise RuntimeError("kaboom")

        # At least the slow_query warning was emitted
        assert any("slow_query" in r.getMessage() for r in caplog.records)


class TestRecordLlmUsage:
    def setup_method(self):
        from app import metrics
        metrics.reset_for_tests()

    def test_increments_counters(self):
        from app.observability import record_llm_usage
        from app import metrics

        record_llm_usage(model="dolphin-24b", tokens_in=120, tokens_out=80,
                         latency_ms=350.0, route="chat")

        c = metrics.snapshot()["counters"]
        assert sum(v for k, v in c.items() if "llm_requests_total" in k) == 1
        assert sum(v for k, v in c.items() if "llm_tokens_in_total" in k) == 120
        assert sum(v for k, v in c.items() if "llm_tokens_out_total" in k) == 80

    def test_records_latency_histogram(self):
        from app.observability import record_llm_usage
        from app import metrics

        record_llm_usage(model="dolphin-24b", tokens_in=10, tokens_out=5,
                         latency_ms=250.0, route="chat")

        h = metrics.snapshot()["histograms"]
        assert any("llm_latency_ms" in k for k in h)

    def test_negative_tokens_clamped_to_zero(self):
        """Badly-formed LLM responses might report negatives; don't crash."""
        from app.observability import record_llm_usage
        from app import metrics

        record_llm_usage(model="x", tokens_in=-5, tokens_out=-1,
                         latency_ms=100.0)

        c = metrics.snapshot()["counters"]
        # Tokens-in/out counters should be 0, not negative
        for k, v in c.items():
            if "llm_tokens" in k:
                assert v >= 0

    def test_swallows_metrics_errors(self):
        """A broken metrics module must not crash llm recording."""
        from app.observability import record_llm_usage

        with patch("app.metrics.incr", side_effect=RuntimeError("m down")):
            # Must not raise
            record_llm_usage(model="x", tokens_in=1, tokens_out=1,
                             latency_ms=10.0)
