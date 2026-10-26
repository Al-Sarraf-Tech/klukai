"""Isolated deterministic no-charge provider HTTP fixtures (stdlib only).

This never calls another service, loads a model or accesses a GPU. Performance
reports cover HTTP/admission/database paths, not real provider/model timing.
There is no voice route: voice is Fish Audio only and is never measured.
"""

from __future__ import annotations
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

COUNTS: Counter[str] = Counter()
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        pass  # No headers, credentials, prompts or query strings in fixture logs.

    def answer(
        self, value: object, *, status: int = 200, session: bool = False
    ) -> None:
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if session:
            self.send_header("Mcp-Session-Id", "isolated-fixture-session")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        with LOCK:
            COUNTS["GET " + path] += 1
        if path == "/fixture-stats":
            self.answer(
                {
                    "fixture": "klukai-deterministic-v1",
                    "counts": dict(COUNTS),
                    "external_calls": 0,
                    "gpu_calls": 0,
                }
            )
        elif path == "/v1/models":  # LM Studio: pre-2026-10-26 baseline only
            self.answer(
                {
                    "data": [
                        {
                            "id": "cognitivecomputations_dolphin-mistral-24b-venice-edition"
                        }
                    ]
                }
            )
        elif path == "/health":  # LM Studio gateway: pre-2026-10-26 baseline only
            self.answer({"status": "ok", "game_active": False})
        elif path == "/memory/recall":
            self.answer({"found": False, "entries": []})
        else:
            self.answer({"error": "Fixture path not implemented"}, status=404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        with LOCK:
            COUNTS["POST " + path] += 1
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/mcp":
            method = body.get("method")
            result = (
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "serverInfo": {"name": "isolated-fixture", "version": "1"},
                }
                if method == "initialize"
                else {"tools": []}
                if method == "tools/list"
                else {"content": []}
            )
            self.answer(
                {"jsonrpc": "2.0", "id": body.get("id"), "result": result}, session=True
            )
        elif path == "/v1/embeddings":  # pre-2026-10-26 baseline only
            self.answer({"data": [{"embedding": [1.0] + [0.0] * 767}]})
        elif path == "/v1/chat/completions":  # pre-2026-10-26 baseline only
            if body.get("stream"):
                raw = b'data: {"choices":[{"delta":{"content":"Deterministic isolated fixture."}}]}\n\ndata: [DONE]\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self.answer(
                    {
                        "model": "isolated-fixture",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "Deterministic isolated fixture.",
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                )
        elif path == "/memory/store":
            self.answer({"status": "ok"})
        else:
            self.answer({"error": "Fixture path not implemented"}, status=404)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8088), Handler).serve_forever()
