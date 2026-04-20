"""Tests for metrics module + /api/metrics endpoint."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _mk_request() -> MagicMock:
    req = MagicMock()
    req.headers = {"Authorization": "Bearer good"}
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    return req


def _app_with_routes() -> FastAPI:
    from app.routes import register_routes
    app = FastAPI()
    register_routes(app)
    return app


def _find_route(app: FastAPI, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


# ═══════════════════════════════════════════════════════════════════════════
# metrics module primitives
# ═══════════════════════════════════════════════════════════════════════════


class TestCounters:
    def setup_method(self):
        from app.metrics import reset_for_tests
        reset_for_tests()

    def test_incr_default_by_1(self):
        from app.metrics import incr, snapshot
        incr("test_count")
        assert snapshot()["counters"]["test_count"] == 1

    def test_incr_with_amount(self):
        from app.metrics import incr, snapshot
        incr("test_count", by=5)
        incr("test_count", by=3)
        assert snapshot()["counters"]["test_count"] == 8

    def test_incr_with_labels_keyed_compound(self):
        from app.metrics import incr, snapshot
        incr("requests", path="/a", status="200")
        incr("requests", path="/a", status="200")
        incr("requests", path="/a", status="500")
        c = snapshot()["counters"]
        assert c["requests{path=/a,status=200}"] == 2
        assert c["requests{path=/a,status=500}"] == 1


class TestHistogram:
    def setup_method(self):
        from app.metrics import reset_for_tests
        reset_for_tests()

    def test_observe_places_in_correct_bucket(self):
        from app.metrics import observe_latency, snapshot
        observe_latency("lat", 7.5)  # 10ms bucket
        observe_latency("lat", 150)  # 250ms bucket
        observe_latency("lat", 5000) # 5000ms bucket
        h = snapshot()["histograms"]["lat"]
        assert h["buckets"]["10"] == 1
        assert h["buckets"]["250"] == 1
        assert h["buckets"]["5000"] == 1
        assert h["count"] == 3

    def test_overflow_goes_to_inf(self):
        from app.metrics import observe_latency, snapshot
        observe_latency("lat", 99999)
        h = snapshot()["histograms"]["lat"]
        assert h["buckets"]["inf"] == 1

    def test_avg_computed_correctly(self):
        from app.metrics import observe_latency, snapshot
        for v in (10, 20, 30):
            observe_latency("lat", v)
        h = snapshot()["histograms"]["lat"]
        assert h["count"] == 3
        assert h["sum_ms"] == 60
        assert h["avg_ms"] == 20


class TestSnapshot:
    def setup_method(self):
        from app.metrics import reset_for_tests
        reset_for_tests()

    def test_snapshot_has_uptime(self):
        from app.metrics import snapshot
        snap = snapshot()
        assert "uptime_seconds" in snap
        assert snap["uptime_seconds"] >= 0

    def test_empty_snapshot_shape(self):
        from app.metrics import snapshot
        snap = snapshot()
        assert snap["counters"] == {}
        assert snap["histograms"] == {}


# ═══════════════════════════════════════════════════════════════════════════
# /api/metrics endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestMetricsEndpoint:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/metrics", "GET")
        with patch("app.routes._get_user_id", return_value=None):
            resp = await handler(_mk_request())
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_forbidden(self):
        app = _app_with_routes()
        handler = _find_route(app, "/api/metrics", "GET")
        with patch("app.routes._get_user_id", return_value="bob"):
            resp = await handler(_mk_request())
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_returns_snapshot(self):
        from app.metrics import incr, reset_for_tests
        reset_for_tests()
        incr("endpoint_hit", route="stats")

        app = _app_with_routes()
        handler = _find_route(app, "/api/metrics", "GET")

        fake_pool = MagicMock()
        fake_pool.min_size = 2
        fake_pool.max_size = 10

        with patch("app.routes._get_user_id", return_value="jalsarraf"), \
             patch("app.routes.get_pool", return_value=fake_pool):
            data = await handler(_mk_request())

        assert "uptime_seconds" in data
        assert "counters" in data
        assert "histograms" in data
        assert data["db_pool"]["min_size"] == 2
        assert data["db_pool"]["max_size"] == 10
