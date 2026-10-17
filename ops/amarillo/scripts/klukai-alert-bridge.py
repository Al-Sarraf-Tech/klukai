#!/usr/bin/env python3
"""Deliver Alertmanager webhooks to the owner as bus notes (Tailscale-only bus).

Alertmanager runs in the klukai-obs Docker network and posts to this host
process on that network's gateway address. Each webhook batch becomes one
compact `bus send owner@amarillo --note`. Grouping, repeat and resolve timing
stay in Alertmanager; a failed bus send returns 502 so it retries.

Never put secrets in a note: only alert names, labels and annotations from the
generated SLO rules are used, bounded and stripped of control characters.
"""

from __future__ import annotations

import argparse
import hmac
import http.server
import json
import os
import stat
import subprocess
import sys
from typing import Any

MAX_BODY = 256 * 1024
MAX_NOTE = 900


def read_token(path: str) -> str:
    """The bearer token file must be a private regular file (0600-style)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Alert bridge token file must be private and regular")
        token = source.read(4097).strip()
    if not token or len(token) > 4096:
        raise ValueError("Alert bridge token file is empty or oversized")
    return token


def _clean(value: object, limit: int = 200) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return " ".join(text.split())[:limit]


def note(payload: dict[str, Any]) -> str:
    """One line per batch: status, alert names, severity, route, summary, runbook."""
    if payload.get("version") != "4" or not isinstance(payload.get("alerts"), list):
        raise ValueError("Not an Alertmanager v4 webhook")
    alerts = [a for a in payload["alerts"] if isinstance(a, dict)]
    if not alerts:
        raise ValueError("Webhook carries no alerts")
    status = _clean(payload.get("status", "unknown"), 16).upper()
    parts = []
    for alert in alerts[:5]:
        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        notes = (
            alert.get("annotations")
            if isinstance(alert.get("annotations"), dict)
            else {}
        )
        item = _clean(labels.get("alertname", "alert"), 80)
        detail = [
            f"{key}={_clean(labels[key], 60)}"
            for key in ("severity", "dep", "method", "route")
            if labels.get(key)
        ]
        if detail:
            item += " (" + " ".join(detail) + ")"
        if notes.get("summary"):
            item += ": " + _clean(notes["summary"], 160)
        if notes.get("runbook_url"):
            item += " runbook " + _clean(notes["runbook_url"], 160)
        parts.append(item)
    if len(alerts) > 5:
        parts.append(f"+{len(alerts) - 5} more")
    text = f"KLUKAI_ALERT {status}: " + " | ".join(parts)
    return text[:MAX_NOTE]


def deliver(bus: str, target: str, text: str, project: str | None) -> bool:
    argv = [bus, "send", target, "--note", text]
    if project:
        argv += ["--project", project]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def handler(token: str, bus: str, target: str, project: str | None) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "klukai-alert-bridge"
        timeout = 10  # a stalled client cannot pin a thread

        def _reply(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _reject(self, code: int, length: int) -> None:
            # Drain a small body so the client reads the status instead of a
            # reset; anything larger, or any unauthenticated upload beyond a
            # few KiB, just closes the connection.
            if 0 < length <= (4 * MAX_BODY if code == 413 else 16 * 1024):
                self.rfile.read(length)
            else:
                self.close_connection = True
            self._reply(code)

        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            supplied = self.headers.get("Authorization", "")
            if self.path != "/alert" or not hmac.compare_digest(
                supplied.encode(), f"Bearer {token}".encode()
            ):
                self._reject(401 if self.path == "/alert" else 404, length)
                return
            if not 0 < length <= MAX_BODY:
                self._reject(413, length)
                return
            try:
                payload = json.loads(self.rfile.read(length))
                text = note(payload if isinstance(payload, dict) else {})
            except (ValueError, UnicodeDecodeError):
                self._reply(400)
                return
            self._reply(200 if deliver(bus, target, text, project) else 502)

        def log_message(self, fmt: str, *args: object) -> None:
            sys.stderr.write("alert-bridge: " + (fmt % args) + "\n")

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, help="host:port (klukai-obs gateway)")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--bus", default=os.path.expanduser("~/.local/bin/bus"))
    parser.add_argument("--target", default="owner@amarillo")
    parser.add_argument("--project", default=None)
    args = parser.parse_args()
    host, _, port = args.bind.rpartition(":")
    if not host or host in {"0.0.0.0", "::"}:
        parser.error(
            "--bind must name the klukai-obs gateway address, not all interfaces"
        )
    token = read_token(args.token_file)
    server = http.server.ThreadingHTTPServer(
        (host, int(port)), handler(token, args.bus, args.target, args.project)
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
