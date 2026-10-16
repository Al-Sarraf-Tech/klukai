"""Private Prometheus endpoint with a separately scoped fail-closed token."""

from __future__ import annotations
import hmac
import os
import stat
from fastapi import FastAPI, Request
from starlette.responses import Response
from ..metrics import prometheus_text


def _scrape_token() -> str | None:
    path = os.environ.get(
        "KLUKAI_METRICS_TOKEN_FILE", "/run/klukai-secrets/metrics-token"
    )
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_size > 4096
            ):
                return None
            value = source.read(4097).strip()
            return value if value and len(value) <= 4096 else None
    except (OSError, UnicodeError):
        return None


def register_metrics(app: FastAPI) -> None:
    @app.get("/internal/metrics", include_in_schema=False)
    async def scrape(request: Request) -> Response:
        configured = _scrape_token()
        supplied = request.headers.get("authorization", "")
        authorized = supplied.startswith("Bearer ") and configured is not None
        if not authorized or not hmac.compare_digest(
            supplied[7:].encode(), configured.encode() if configured else b""
        ):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return Response(
            prometheus_text(),
            headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
        )
