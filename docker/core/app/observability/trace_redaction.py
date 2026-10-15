"""Sanitize completed spans, including exception events, before OTLP export."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status
from opentelemetry.util.types import Attributes

from .redaction import redact_credentials


def _attributes(attributes: Attributes) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if isinstance(value, str):
            value = redact_credentials(value)
        elif isinstance(value, (tuple, list)):
            value = tuple(
                redact_credentials(v) if isinstance(v, str) else v for v in value
            )
        result[key] = value
    return result


class CredentialRedactingExporter(SpanExporter):
    def __init__(self, exporter: SpanExporter):
        self._exporter = exporter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        sanitized = []
        for span in spans:
            description = span.status.description
            sanitized.append(
                ReadableSpan(
                    name=redact_credentials(span.name),
                    context=span.context,
                    parent=span.parent,
                    resource=span.resource,
                    kind=span.kind,
                    attributes=_attributes(span.attributes),
                    events=[
                        Event(
                            redact_credentials(e.name),
                            _attributes(e.attributes),
                            timestamp=e.timestamp,
                            limit=None,
                        )
                        for e in span.events
                    ],
                    links=[
                        Link(link.context, _attributes(link.attributes))
                        for link in span.links
                    ],
                    status=Status(
                        span.status.status_code,
                        redact_credentials(description) if description else description,
                    ),
                    start_time=span.start_time,
                    end_time=span.end_time,
                    instrumentation_scope=span.instrumentation_scope,
                )
            )
        # Copy, never mutate spans another processor/exporter may own.
        return self._exporter.export(sanitized)

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)
