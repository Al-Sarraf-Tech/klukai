"""Exercise emitted RED data, authenticated scrape, correlation and SLOs."""

from __future__ import annotations
import logging
import re

import httpx
import pytest
from fastapi import FastAPI
from app import metrics
from app.main import _MetricsMiddleware, _RequestIdMiddleware
from app.observability.private_metrics import register_metrics
from app.observability import structured_log
from app.observability import slos, tracing


def make_app():
    app = FastAPI()

    @app.get("/resource/{resource_id}")
    async def resource(resource_id: str):
        return {"ok": True}

    app.add_middleware(_RequestIdMiddleware)
    app.add_middleware(_MetricsMiddleware)
    register_metrics(app)
    return app


@pytest.mark.asyncio
async def test_red_cardinality_uses_route_templates_and_fixed_unknown_bucket():
    metrics.reset_for_tests()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app()), base_url="http://test"
    ) as client:
        for index in range(30):
            await client.get(f"/resource/{index}")
            await client.get(f"/unknown/{index}")
    counters = metrics.snapshot()["counters"]
    requests = {
        key: count
        for key, count in counters.items()
        if key.startswith("http_requests_total")
    }
    assert len(requests) == 2
    assert (
        requests[
            "http_requests_total{method=GET,route=/resource/{resource_id},status_class=2xx}"
        ]
        == 30
    )
    assert (
        requests["http_requests_total{method=GET,route=_unmatched,status_class=4xx}"]
        == 30
    )
    assert "unknown/1" not in str(metrics.snapshot())


@pytest.mark.asyncio
async def test_private_scrape_is_scoped_fail_closed_and_prometheus(
    tmp_path, monkeypatch
):
    token = tmp_path / "metrics-token"
    monkeypatch.setenv("KLUKAI_METRICS_TOKEN_FILE", str(token))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app()), base_url="http://test"
    ) as client:
        assert (await client.get("/internal/metrics")).status_code == 401
        token.write_text("scoped-test-token")
        token.chmod(0o400)
        assert (
            await client.get(
                "/internal/metrics", headers={"Authorization": "Bearer wrong"}
            )
        ).status_code == 401
        response = await client.get(
            "/internal/metrics", headers={"Authorization": "Bearer scoped-test-token"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
        assert "klukai_http_requests_total{" in response.text
        token.chmod(0o644)
        assert (
            await client.get(
                "/internal/metrics",
                headers={"Authorization": "Bearer scoped-test-token"},
            )
        ).status_code == 401
        token.chmod(0o600)
        token.write_text("")
        token.chmod(0o400)
        assert (
            await client.get(
                "/internal/metrics",
                headers={"Authorization": "Bearer scoped-test-token"},
            )
        ).status_code == 401


def test_prometheus_histograms_are_cumulative_and_breakers_emit():
    from app.circuit_breakers import get_breaker

    metrics.reset_for_tests()
    metrics.observe_latency("sample_ms", 7)
    metrics.observe_latency("sample_ms", 20)
    get_breaker("weather").force_open()
    output = metrics.prometheus_text()
    assert 'klukai_sample_ms_bucket{le="10"} 1' in output
    assert 'klukai_sample_ms_bucket{le="25"} 2' in output
    assert 'klukai_sample_ms_bucket{le="+Inf"} 2' in output
    assert 'klukai_circuit_state{dep="weather"} 2' in output
    assert 'klukai_circuit_transitions_total{dep="weather",state="open"} 1' in output


@pytest.mark.asyncio
async def test_request_id_rejects_credential_like_values_and_restores_context(caplog):
    from app.observability.correlation import request_id_context

    app = make_app()

    @app.get("/logged")
    async def logged():
        structured_log(
            logging.getLogger("correlation-test"),
            logging.WARNING,
            "request",
            url="/?token=private",
        )
        return {}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/logged", headers={"X-Request-ID": "Bearer private"}
        )
    rid = response.headers["X-Request-ID"]
    assert re.fullmatch("[0-9a-f]{12}", rid)
    assert rid in caplog.text and "private" not in caplog.text
    assert request_id_context.get() is None


def test_slo_routes_and_recording_rules_match_served_surface():
    from app.main import app

    routes = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", ())
    }
    slos.validate_routes(routes)
    assert len(slos.recording_rules()) == 2 * len(slos.endpoint_slos())
    assert "/api/health/ready" in slos.markdown_table()
    with pytest.raises(ValueError):
        slos.validate_routes(set())


@pytest.mark.parametrize(
    "raw, expected", [("nan", 0.1), ("bad", 0.1), ("-1", 0), ("2", 1), (".25", 0.25)]
)
def test_explicit_sampling_is_bounded(monkeypatch, raw, expected):
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", raw)
    assert tracing.sampling_ratio() == expected


def test_trace_export_contains_sampled_redacted_correlation():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    from app.observability.trace_redaction import CredentialRedactingExporter

    output = InMemorySpanExporter()
    provider = TracerProvider(sampler=ParentBased(TraceIdRatioBased(1)))
    provider.add_span_processor(
        SimpleSpanProcessor(CredentialRedactingExporter(output))
    )
    with provider.get_tracer("test").start_as_current_span("request") as span:
        tracing.correlate_request("safe-request-id")
        span.set_attribute("url.full", "http://test/?token=secret-value")
    (exported,) = output.get_finished_spans()
    assert exported.attributes["klukai.request_id"] == "safe-request-id"
    assert exported.attributes["url.full"] == "http://test/?token=***"
    provider.shutdown()


def test_prometheus_preserves_trailing_template_braces():
    metrics.reset_for_tests()
    metrics.record_http("/api/tts/jobs/{job_id}", "GET", 200, 2)
    text = metrics.prometheus_text()
    assert 'route="/api/tts/jobs/{job_id}"' in text
    assert 'route="/api/tts/jobs/{job_id"' not in text
