"""Shell-free readiness probe; optional provider outages do not disable text."""

from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


def healthy(url: str = "http://127.0.0.1:8300/api/health/ready") -> bool:
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=3) as response:
            if response.status != 200:
                return False
            body = response.read(4097)
        if len(body) > 4096:
            return False
        result = json.loads(body)
        return isinstance(result, dict) and result.get("status") in {"ok", "degraded"}
    except (OSError, URLError, ValueError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy() else 1)
