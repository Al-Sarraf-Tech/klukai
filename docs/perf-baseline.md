# Performance Baseline — klukai

This document records klukai's measured performance and the procedure to
re-capture a baseline. Established as part of the **Phase 1 S-tier
uplift** (see `CHANGELOG.md`).

## Why this exists

Before this baseline, performance was unmeasured (tier rubric: **D**).
With this baseline:

1. Phase 2 PRs can gate on regression (e.g., `>20% p99 delta = fail`).
2. SLOs can be defined from real numbers, not guesses.
3. Future tuning has a starting point to compare against.

## Re-running the baseline

```bash
# Requires klukai-core reachable at the target base URL.
make perf-baseline

# Or directly:
python3 tools/load-test/probe.py \
    --base http://localhost:8300 \
    --requests 200 --concurrency 10 \
    --out docs/perf-baseline.json
```

Exit code: `0` on success, `1` if any endpoint exceeded 5% error rate
(smoke gate). The script writes a JSON artifact suitable for diffing
against future runs.

## What it measures

Unauthenticated endpoints only (Phase 1 scope). Phase 2 adds auth-gated
paths once a CI-friendly seed-user credential pattern is in place.

| Endpoint | Method | Notes |
|---|---|---|
| `/health` | GET | Subsystem health rollup (DB pool, Redis, Qdrant) |
| `/api/health/subsystems` | GET | Per-subsystem detail, includes voice + LM Studio |

## Current baseline (2026-05-16)

Captured on amarillo from localhost, against the running `companion-core`
container. 200 requests per endpoint at concurrency 10. See
`docs/perf-baseline.json` for the raw artifact.

| Endpoint | p50 (ms) | p95 (ms) | p99 (ms) | Errors |
|---|---:|---:|---:|---:|
| `/health` | 120.75 | 128.49 | 133.43 | 0 / 200 |
| `/api/health/subsystems` | 10.94 | 44.22 | 60.38 | 0 / 200 |

## Observations

- **`/health` is the bottleneck.** ~120ms p50 because it inlines a
  synchronous ping of PostgreSQL pool, Redis, and Qdrant before
  responding. This was acceptable when it was an infrequent probe,
  but is the wrong shape for a high-frequency healthcheck (Docker's
  default healthcheck interval is 15s, so latency compounds over time).

  **Phase 2 SLO target:** `/health` p99 ≤ 30ms via subsystem cache
  with 5s TTL.

- **`/api/health/subsystems` is fast** (~11ms p50) despite reporting
  more state, suggesting the subsystem reporting itself is cheap and
  the bottleneck in `/health` is the inline backend pings.

- **Zero errors at concurrency 10.** Healthy. Phase 2 will retest at
  higher concurrency (50, 100) once the cache lands.

## Phase 2 perf gate plan

1. Cache subsystem health checks (5s TTL) so `/health` doesn't ping
   backends on every probe.
2. Add `/api/chat` to the load test with a seed-user bearer token.
   Establish chat-path baseline (probably dominated by LM Studio
   latency on dominus, which is fine — measure it, don't fight it).
3. Wire `make perf-baseline` into CI as a non-gating informational
   step. Compare against `docs/perf-baseline.json` from `main`.
   Print a regression diff but don't fail CI yet.
4. Once stable for a sprint, flip the gate on: `>20% p99 delta = fail`.

## Notes on methodology

- The probe is pure `httpx` + `asyncio.gather` (Python). No locust /
  k6 / vegeta — keeps the harness inside klukai's existing toolchain
  and avoids a new heavy dep in CI.
- `concurrency=10` matches Docker's default `--health-interval=15s`
  + `start_period=10s` envelope without overloading the test box.
- Latencies are wall-clock including network — for localhost on
  amarillo the network is essentially free, so the numbers reflect
  app + backend round-trip.
