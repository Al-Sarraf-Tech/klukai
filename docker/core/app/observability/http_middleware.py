"""HTTP correlation and bounded route-template RED instrumentation."""

from __future__ import annotations
import re
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response as StarletteResponse


class _RequestIdMiddleware(BaseHTTPMiddleware):
    """Inject X-Request-ID on every request. Propagate if client sent one."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> StarletteResponse:
        import uuid

        rid = request.headers.get("X-Request-ID", "") or ""
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid) is None:
            rid = uuid.uuid4().hex[:12]
        from .correlation import request_id_context

        token = request_id_context.set(rid)
        from .tracing import correlate_request

        correlate_request(rid)
        request.state.request_id = rid
        try:
            response: StarletteResponse = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            request_id_context.reset(token)


class _MetricsMiddleware(BaseHTTPMiddleware):
    """Count requests and record latency for every handled request."""

    def _metric_path(self, request: Request, status: int | None = None) -> str:
        """Label paths only for registered routes — raw URL paths from scans
        and 404s would otherwise grow metric label cardinality without bound."""
        matched = request.scope.get("route")
        matched_path = getattr(matched, "path", None)
        if isinstance(matched_path, str):
            return matched_path
        path = request.url.path
        for route in request.app.routes:
            route_path = getattr(route, "path", None)
            if not isinstance(route_path, str):
                continue
            regex = getattr(route, "path_regex", None)
            if route_path == path or (regex is not None and regex.match(path)):
                return route_path
        return "_unmatched"

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> StarletteResponse:
        import time
        from .. import metrics

        start = time.monotonic()
        try:
            response: StarletteResponse = await call_next(request)
        except Exception:
            elapsed_ms = (time.monotonic() - start) * 1000
            path_label = self._metric_path(request)
            metrics.record_http(path_label, request.method, 500, elapsed_ms)
            metrics.incr("requests_total", path=path_label, status="500")
            metrics.observe_latency("request_latency_ms", elapsed_ms, path=path_label)
            raise
        elapsed_ms = (time.monotonic() - start) * 1000
        path_label = self._metric_path(request)
        metrics.record_http(
            path_label, request.method, response.status_code, elapsed_ms
        )
        metrics.incr(
            "requests_total", path=path_label, status=str(response.status_code)
        )
        metrics.observe_latency("request_latency_ms", elapsed_ms, path=path_label)
        return response
