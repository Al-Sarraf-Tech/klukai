"""Shell-free liveness for autoheal; --readiness checks required dependencies.

Docker runs this every 15 seconds inside the core's CPU budget. The image has no
precompiled standard library, so urllib/json imports cost about 250 ms of CPU per
probe; only C modules are used here (about the interpreter's bare startup cost).
"""

import _json
import _socket
import sys

_MAX_BODY = 4096


class _Strict:
    """Decoder settings for _json's C scanner (matches json.loads defaults)."""

    strict = True
    object_hook = None
    object_pairs_hook = None
    parse_float = float
    parse_int = int

    @staticmethod
    def parse_constant(name: str) -> None:
        raise ValueError(name)


def _document(text: str) -> object:
    scan = _json.make_scanner(_Strict())
    start = len(text) - len(text.lstrip())
    value, end = scan(text, start)
    if text[end:].strip():
        raise ValueError("trailing data")
    return value


def _get(host: str, port: int, path: str) -> tuple[int, bytes]:
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        sock.settimeout(3)
        sock.connect((host, port))
        request = f"GET {path} HTTP/1.0\r\nHost: {host}:{port}\r\n\r\n"
        sock.sendall(request.encode("ascii"))
        raw = b""
        while len(raw) <= _MAX_BODY + 8192:
            chunk = sock.recv(8192)
            if not chunk:
                break
            raw += chunk
    finally:
        sock.close()
    head, separator, body = raw.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].split(b" ")
    if not separator or len(status) < 2 or not status[0].startswith(b"HTTP/1."):
        raise ValueError("malformed response")
    return int(status[1]), body


def healthy(url: str = "http://127.0.0.1:8300/api/health/live") -> bool:
    try:
        if not url.startswith("http://"):
            return False
        authority, _, path = url[len("http://") :].partition("/")
        host, _, port = authority.rpartition(":")
        status, body = _get(host, int(port), "/" + path)
        if status != 200 or len(body) > _MAX_BODY:
            return False
        result = _document(body.decode("utf-8"))
        return isinstance(result, dict) and result.get("status") in {"ok", "degraded"}
    except (OSError, ValueError, StopIteration, UnicodeError):
        return False


if __name__ == "__main__":
    endpoint = "ready" if sys.argv[1:] == ["--readiness"] else "live"
    if sys.argv[1:] not in ([], ["--readiness"]):
        raise SystemExit(2)
    raise SystemExit(
        0 if healthy(f"http://127.0.0.1:8300/api/health/{endpoint}") else 1
    )
