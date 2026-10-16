"""Shell-free liveness for autoheal; --readiness checks required dependencies."""

from __future__ import annotations

import json
import argparse
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


def healthy(url: str = "http://127.0.0.1:8300/api/health/live") -> bool:
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=3) as response:
            if response.status != 200:
                return False
            body = response.read(4097)
        if len(body) > 4096:
            return False
        result = json.loads(body)
        return isinstance(result, dict) and result.get("status") in {"ok", "degraded"}
    except HTTPError as exc:
        exc.close()
        return False
    except (OSError, URLError, ValueError):
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readiness", action="store_true")
    args = parser.parse_args()
    endpoint = "ready" if args.readiness else "live"
    raise SystemExit(
        0 if healthy(f"http://127.0.0.1:8300/api/health/{endpoint}") else 1
    )
