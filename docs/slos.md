# Service Level Objectives — klukai

This document codifies klukai's per-endpoint SLOs (Service Level
Objectives). Phase 2 deliverable for the S+ tier uplift (see
`docs/superpowers/specs/2026-05-16-s-plus-uplift.md` §6.6).

The baseline numbers come from `docs/perf-baseline.md` (Phase 1 perf
probe). The targets below are what klukai commits to over a rolling
30-day window. SLO breach → page → root-cause → either fix or
adjust the target (with an ADR).

## Why SLOs matter

Without them, performance is whatever it happens to be on the day
someone checks. With them, every PR is gated against a number, every
alert links to an action, and an "incident" is a measurable thing
instead of a vibe.

## Conventions

- **Latency**: wall-clock from request-receive to response-flush at the
  amarillo gateway. Excludes Cloudflare TLS overhead and end-user network.
- **Availability**: 1 − (5xx responses / total responses) over the
  rolling window.
- **Error budget**: `1 − SLO`. A 99.9% availability target gives a 0.1%
  budget — 43m of downtime/month. Burn-rate alerts fire when 14d of
  budget is consumed in < 24h.
- **Rolling window**: 30 days unless otherwise noted.
- **Measurement source**: Prometheus scrape of `/api/metrics` (Phase 2
  wires this in). Pre-Phase 2 the baseline comes from `tools/load-test/probe.py`.

## Per-endpoint SLOs

### Health endpoints

| Endpoint | Latency target | Availability | Notes |
|---|---|---|---|
| `/health` | p99 ≤ **30ms** | 99.99% | Cached (5s TTL). Phase 1 baseline 119ms p50 — must improve via `health_cache.py` |
| `/api/health/live` | p99 ≤ **5ms** | 99.999% | Process-only, no backend ping. Sub-ms expected |
| `/api/health/ready` | p99 ≤ **500ms** | 99.9% | Uncached deep check; readiness probes only |
| `/api/health/subsystems` | p99 ≤ **100ms** | 99.9% | Operator/dashboard endpoint, less hot than `/health` |

### Chat path (dominated by LLM)

| Endpoint | Latency target | Availability | Notes |
|---|---|---|---|
| `POST /api/chat/turn` (first token) | p99 ≤ **2s** | 99.9% | Time-to-first-token; LM Studio on dominus dominates |
| `POST /api/chat/turn` (full response) | p99 ≤ **8s** | 99.9% | Whole completion, varies by `max_tokens` (6144 cap) |
| `WS /ws` (connection) | p99 ≤ **200ms** | 99.9% | Reconnect-tolerant; brief drops acceptable |
| `WS /ws` (per-message latency) | p99 ≤ **8s** | 99.9% | Inherits chat-turn target |

### Memory pipeline

| Endpoint | Latency target | Availability | Notes |
|---|---|---|---|
| `POST /api/memories/search` | p99 ≤ **500ms** | 99.9% | Qdrant vector search |
| `GET /api/memories/timeline` | p99 ≤ **300ms** | 99.9% | PG read with index |
| `GET /api/memories/affection-timeline` | p99 ≤ **300ms** | 99.9% | PG read with date aggregation |

### User-facing CRUD

| Endpoint | Latency target | Availability | Notes |
|---|---|---|---|
| `GET /api/user/stats` | p99 ≤ **200ms** | 99.9% | PG aggregation |
| `GET /api/user/export` | p99 ≤ **3s** | 99.9% | Full data dump; lower frequency |
| `POST /api/user/login` | p99 ≤ **300ms** | 99.95% | bcrypt verify + session create |
| `POST /api/user/password-change` | p99 ≤ **300ms** | 99.95% | Same shape as login |

### Audit + admin

| Endpoint | Latency target | Availability | Notes |
|---|---|---|---|
| `POST /api/audit/log` (internal) | p99 ≤ **100ms** | 99.95% | PG write; HMAC chain extend |
| `GET /api/audit/verify` | p99 ≤ **500ms** | 99.9% | Walk chain; O(N) over recent window |
| `POST /api/admin/rate-limit/reset` | p99 ≤ **200ms** | 99.9% | Redis flush by bucket |

### Image gen (offloaded to dominus)

| Endpoint | Latency target | Availability | Notes |
|---|---|---|---|
| `POST /api/images/generate` (queue) | p99 ≤ **200ms** | 99.9% | Just enqueues; actual gen happens async |
| Image gen end-to-end | p95 ≤ **15s** | 99% | ComfyUI on dominus; 5s VRAM-contention delay built in |

## Composite SLOs

| Composite | Target |
|---|---|
| **Overall service availability** | 99.9% — measured as the rate of non-5xx responses across all endpoints |
| **Chat round-trip availability** | 99.9% — chat must work even if image gen is down |
| **Cold-start recovery** | After `docker compose restart`, `/health` returns `ok` within **30s** |

## Burn-rate alerting

Alerts fire when the 30-day error budget burns faster than expected:

- **Fast burn (page)**: 14d budget consumed in 1h → wake someone.
- **Slow burn (ticket)**: 7d budget consumed in 24h → file an issue.

Burn-rate formula (per https://sre.google/workbook/alerting-on-slos/):

```
short_window_burn = (errors_in_short_window / total_in_short_window) / (1 - SLO)
long_window_burn  = (errors_in_long_window  / total_in_long_window)  / (1 - SLO)
alert_if  short_burn ≥ 14 AND long_burn ≥ 14
```

The Prometheus alert rules in `docs/alerts/klukai.yaml` (Phase 2)
encode the math. Annotations link to `docs/runbooks/<name>.md` so
the on-call page lands you on the right runbook.

## Out-of-SLO endpoints (explicitly named)

These endpoints do NOT have an SLO because they're operator-only or
intrinsically variable:

- `/api/admin/*` — operator-only, error rate matters but latency doesn't.
- `/api/voice/clone-sample` — one-off, training-time bound.
- `POST /api/memory-archive/seed` — nightly batch, runs for minutes by design.

If one of these becomes user-facing, it gets an SLO when that PR lands.

## Revisiting

This document is **not a contract** — it's a target. When the architecture
changes (e.g., voice moves off dominus, LM Studio replaced with a faster
model, cache TTL tuned), the targets get re-evaluated. Change history
lives in `git log -- docs/slos.md`; rationale for any target change goes
in an ADR.

## Phase 2 perf-gate plan

Once SLOs land, `tools/load-test/probe.py` extends to:
1. Compare against `docs/perf-baseline.json`.
2. Fail PR if any endpoint's p99 worsens by > 20% AND breaches its SLO.
3. Print a regression diff on every CI run as an informational step
   even when not failing.

This makes regressions visible at author-time, not in production.
