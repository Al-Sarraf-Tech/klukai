"""Credentials must not reach the real server log or trace pipelines."""

import io
import logging

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from app import main
from app.observability import tracing


@pytest.mark.parametrize("logger_name", ["uvicorn.error", "uvicorn.access", "httpx"])
@pytest.mark.parametrize("key", ["token", "TOKEN", "%74oken"])
def test_actual_logger_handlers_never_emit_websocket_credentials(logger_name, key):
    logger = logging.getLogger(logger_name)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        # Uvicorn uses uvicorn.error for handshake logs, not uvicorn.access.
        logger.info(
            '%s - "WebSocket %s" [accepted]',
            "127.0.0.1",
            f"/ws?{key}=fixture-secret&keep=yes",
        )
        assert "fixture-secret" not in stream.getvalue()
        assert "keep=yes" in stream.getvalue()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.mark.parametrize(
    "msg,args",
    [
        ("/ws?token=fixture-secret", ()),
        ("/ws?token=%s", ("fixture-secret",)),
        ("/ws?token=%(credential)s", ({"credential": "fixture-secret"},)),
    ],
)
def test_filter_handles_preformatted_and_split_credentials(msg, args):
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1, msg, args, None
    )
    main._RedactTokenLogFilter().filter(record)
    assert "fixture-secret" not in record.getMessage()


def test_error_traceback_redacts_url_without_mutating_the_exception():
    error = ValueError("/ws?token=traceback-secret&keep=yes")
    try:
        raise error
    except ValueError:
        import sys

        record = logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            __file__,
            1,
            "ASGI application failed",
            (),
            sys.exc_info(),
        )
    main._RedactTokenLogFilter().filter(record)
    rendered = logging.Formatter().format(record)
    assert "traceback-secret" not in rendered
    assert "ValueError" in rendered and "keep=yes" in rendered
    assert "traceback-secret" in str(error)


@pytest.mark.parametrize(
    "url",
    [
        "/ws?search-token=public&keep=yes",
        "/ws?other.token=public",
        "/ws?search:token=public",
        "/ws?search+token=public",
        "/ws?search/token=public",
    ],
)
def test_noncredential_query_keys_are_preserved(url):
    from app.observability.redaction import redact_credentials

    assert redact_credentials(url) == url


def test_nested_url_values_and_cached_tracebacks_are_sanitized():
    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        __file__,
        1,
        "request url=/ws?token=nested-secret&keep=yes",
        (),
        None,
    )
    record.exc_text = "ValueError: /ws?token=cached-secret&keep=yes"
    main._RedactTokenLogFilter().filter(record)
    output = logging.Formatter().format(record)
    assert "nested-secret" not in output and "cached-secret" not in output
    assert "keep=yes" in output


def test_bearer_header_text_is_redacted_in_errors():
    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        __file__,
        1,
        "request failed: Authorization: " + "Bearer " + "header-fixture-secret",
        (),
        None,
    )
    main._RedactTokenLogFilter().filter(record)
    assert "header-fixture-secret" not in record.getMessage()


def test_real_fastapi_traces_redact_credentials_even_with_header_capture(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider)
    monkeypatch.setattr(tracing, "_TRACING_INITIALIZED", True)
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST",
        "authorization,cookie",
    )
    app = FastAPI()

    @app.websocket("/ws")
    async def socket(ws: WebSocket):
        # Sanitization must retain the real credential for authentication.
        assert ws.query_params["token"] == "trace-secret"
        await ws.accept()
        await ws.close()

    assert tracing.instrument_fastapi(app)
    try:
        with TestClient(app) as client:
            with client.websocket_connect(
                "/ws?token=trace-secret&keep=yes",
                headers={
                    "Authorization": "Bearer header-secret",
                    "Cookie": "session=cookie-secret",
                },
            ):
                pass
        spans = exporter.get_finished_spans()
        assert spans
        attributes = repr([dict(span.attributes or {}) for span in spans])
        assert "trace-secret" not in attributes
        assert "header-secret" not in attributes
        assert "cookie-secret" not in attributes
        assert "keep=yes" in attributes
    finally:
        provider.shutdown()


def test_real_configured_export_pipeline_redacts_exception_events(monkeypatch):
    # Export-pipeline verification needs every test span; production defaults to10%.
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "1")
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc import trace_exporter
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    monkeypatch.setattr(tracing, "_TRACING_INITIALIZED", False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://not-used:4317")
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda **_: exporter)
    monkeypatch.setattr(
        trace,
        "set_tracer_provider",
        lambda p: monkeypatch.setattr(trace, "_TRACER_PROVIDER", p),
    )
    assert tracing.init_tracing()
    provider = trace.get_tracer_provider()
    app = FastAPI()

    @app.websocket("/ws")
    async def socket(ws: WebSocket):
        await ws.accept()
        raise ValueError(str(ws.url))

    assert tracing.instrument_fastapi(app)
    try:
        with TestClient(app) as client:
            with pytest.raises(ValueError):
                with client.websocket_connect("/ws?token=event-secret&keep=yes"):
                    pass
        provider.force_flush()
        spans = exporter.get_finished_spans()
        assert any(span.events for span in spans)
        exported = repr(
            [
                (
                    span.name,
                    span.attributes,
                    span.status.description,
                    [(e.name, e.attributes) for e in span.events],
                )
                for span in spans
            ]
        )
        assert "event-secret" not in exported
        assert "ValueError" in exported and "keep=yes" in exported
    finally:
        provider.shutdown()


def test_exporter_sanitizes_sequence_attributes_without_mutating_original():
    from opentelemetry.sdk.trace import ReadableSpan, Event
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.trace import Status, StatusCode
    from app.observability.trace_redaction import CredentialRedactingExporter

    sink = InMemorySpanExporter()
    exporter = CredentialRedactingExporter(sink)
    source = ReadableSpan(
        "fixture",
        attributes={"referer": ("/ws?token=sequence-secret&keep=yes",)},
        events=[Event("error", {"exception.message": "/ws?token=event-secret"})],
        status=Status(StatusCode.ERROR, "/ws?token=status-secret"),
    )
    exporter.export([source])
    exported = sink.get_finished_spans()[0]
    assert all(
        secret
        not in repr(
            (
                exported.attributes,
                exported.events[0].attributes,
                exported.status.description,
            )
        )
        for secret in ("sequence-secret", "event-secret", "status-secret")
    )
    assert "sequence-secret" in repr(source.attributes)
    assert "keep=yes" in repr(exported.attributes)
    assert exporter.force_flush()
    exporter.shutdown()
