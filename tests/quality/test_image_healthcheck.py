"""The shell-free image probe verifies required backend readiness."""

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
