"""The shell-free image probe verifies required backend readiness."""

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

import pytest


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, b'{"status":"ok"}', True),
        (200, b'{"status":"degraded"}', True),
        (200, b'{"status":"unhealthy"}', False),
        (503, b'{"status":"unhealthy"}', False),
        (200, b"not-json", False),
        (200, b"[]", False),
    ],
)
def test_readiness_requires_valid_noncritical_status(
    status, body, expected, monkeypatch
):
    path = Path(__file__).resolve().parents[2] / "docker/core/healthcheck.py"
    assert path.is_file(), "shell-free readiness check is missing"
    spec = importlib.util.spec_from_file_location("image_healthcheck", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/health/ready"
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
        try:
            assert (
                module.healthy(
                    f"http://127.0.0.1:{server.server_port}/api/health/ready"
                )
                is expected
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_autoheal_liveness_stays_healthy_during_required_backend_outage(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "docker/core/healthcheck.py"
    spec = importlib.util.spec_from_file_location("image_healthcheck_liveness", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            live = self.path == "/api/health/live"
            self.send_response(200 if live else 503)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}' if live else b'{"status":"unhealthy"}')

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real = build_opener(ProxyHandler({}))

        class LoopbackPort:
            def open(self, url, **kwargs):
                return real.open(
                    f"http://127.0.0.1:{server.server_port}{urlsplit(url).path}",
                    **kwargs,
                )

        monkeypatch.setattr(module, "build_opener", lambda *_: LoopbackPort())
        try:
            assert module.healthy() is True, (
                "Backend loss must not trigger an autoheal restart loop"
            )
            assert module.healthy("http://127.0.0.1:8300/api/health/ready") is False
        finally:
            server.shutdown()
            thread.join(timeout=2)
