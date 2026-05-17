#!/usr/bin/env python3
"""Async HTTP load probe for klukai.

Targets a list of endpoints, hits each with N requests at concurrency C,
prints p50/p95/p99 + error rate, and writes a baseline JSON suitable for
diffing against future runs (Phase 2 will gate PRs on regression).

Pure stdlib + httpx (already in klukai's requirements.txt). No locust /
k6 / heavy harness — keeps the perf gate inside the project ecosystem
and CI-image-friendly.

Usage:
    python3 tools/load-test/probe.py --base http://localhost:8300 \\
        --requests 200 --concurrency 10 \\
        --out docs/perf-baseline.json

By default targets unauthenticated endpoints only. Phase 2 will add
bearer-token-aware auth-gated paths once the seed user creds are wired
into a secrets file (out of scope here).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

# Endpoints to probe. Each is (label, method, path).
# Only unauth or low-auth endpoints — auth-gated ones added in Phase 2.
TARGETS: list[tuple[str, str, str]] = [
    ("health",            "GET", "/health"),
    ("health-subsystems", "GET", "/api/health/subsystems"),
]


@dataclass
class EndpointResult:
    label: str
    path: str
    method: str
    requests: int
    errors: int
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else float("nan")

    @property
    def p95(self) -> float:
        if not self.latencies_ms:
            return float("nan")
        n = len(self.latencies_ms)
        return sorted(self.latencies_ms)[min(int(n * 0.95), n - 1)]

    @property
    def p99(self) -> float:
        if not self.latencies_ms:
            return float("nan")
        n = len(self.latencies_ms)
        return sorted(self.latencies_ms)[min(int(n * 0.99), n - 1)]

    @property
    def mean(self) -> float:
        return statistics.mean(self.latencies_ms) if self.latencies_ms else float("nan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "method": self.method,
            "path": self.path,
            "requests": self.requests,
            "errors": self.errors,
            "error_rate": (self.errors / self.requests) if self.requests else 0.0,
            "latency_ms": {
                "p50": round(self.p50, 2),
                "p95": round(self.p95, 2),
                "p99": round(self.p99, 2),
                "mean": round(self.mean, 2),
            },
        }


async def _hit(client: httpx.AsyncClient, method: str, url: str, result: EndpointResult) -> None:
    start = time.perf_counter()
    try:
        resp = await client.request(method, url, timeout=10.0)
        ms = (time.perf_counter() - start) * 1000
        result.latencies_ms.append(ms)
        if resp.status_code >= 500:
            result.errors += 1
    except Exception:
        result.errors += 1


async def run_target(base: str, label: str, method: str, path: str,
                     n: int, concurrency: int) -> EndpointResult:
    result = EndpointResult(label=label, path=path, method=method, requests=n, errors=0)
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(base_url=base) as client:
        async def _bounded() -> None:
            async with sem:
                await _hit(client, method, path, result)

        await asyncio.gather(*[_bounded() for _ in range(n)])

    return result


async def main_async(args: argparse.Namespace) -> int:
    print(f"Probing {args.base} — {args.requests} req @ concurrency {args.concurrency}")
    results: list[EndpointResult] = []
    for label, method, path in TARGETS:
        r = await run_target(args.base, label, method, path, args.requests, args.concurrency)
        results.append(r)
        print(f"  {label:24s} p50={r.p50:6.2f}ms  p95={r.p95:6.2f}ms  "
              f"p99={r.p99:6.2f}ms  errors={r.errors}/{r.requests}")

    payload = {
        "base_url": args.base,
        "requests_per_endpoint": args.requests,
        "concurrency": args.concurrency,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": [r.to_dict() for r in results],
    }

    if args.out:
        with open(args.out, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"Wrote baseline → {args.out}")

    # Exit 1 if any endpoint had >5% error rate (smoke gate)
    for r in results:
        rate = (r.errors / r.requests) if r.requests else 0.0
        if rate > 0.05:
            print(f"FAIL: {r.label} error_rate={rate:.1%} (>5%)", file=sys.stderr)
            return 1

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="http://localhost:8300",
                   help="Base URL (default: http://localhost:8300)")
    p.add_argument("--requests", type=int, default=200,
                   help="Requests per endpoint (default: 200)")
    p.add_argument("--concurrency", type=int, default=10,
                   help="Concurrent in-flight requests (default: 10)")
    p.add_argument("--out", default=None,
                   help="Write baseline JSON to this path")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
