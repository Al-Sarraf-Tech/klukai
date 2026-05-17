# S+ Tier Uplift Design — klukai

- **Date:** 2026-05-16
- **Status:** Draft (pending user approval)
- **Authors:** jalsarraf, Claude (Opus 4.7, 1M context)
- **ADR:** ADR-0001 (to be created on approval)
- **Supersedes:** ad-hoc tier work in `docs/superpowers/plans/2026-04-10-a-minus-sweep.md`
- **Related:**
  - `~/.claude/TIER_RUBRIC.md` (rubric this plan walks against)
  - `~/git/mcpservers/docs/superpowers/specs/2026-04-19-s-tier-uplift-design.md` (reference pattern)
  - `CHANGELOG.md` Phase 1 entries (`8679409`, `9fadedf`, `4dfa3e9`, `8a1a993`)
  - `docs/perf-baseline.md` (Phase 1 perf baseline)

---

## 1. Summary

klukai is the production AI companion deployed at `klukai.appnest.cc/app/` and consumed by 4 seed users (jalsarraf, ricky, miguel, blackman). Phase 1 lifted the project from **D-floor (perf unmeasured)** to **C+ tier** by adding mypy + bandit + safety CI gates, loopback bind, autoheal labels, a CHANGELOG, and a perf baseline.

This document specifies the path from **C+ → S+ tier**. S+ means: defensible against external audit, useful as a reference architecture, survives 5+ years of maintenance with rotating eyes, and `scripts/s-tier-audit.sh` returns green on every dimension.

The plan is **dimension-by-dimension**, not pilot-then-rollout (klukai is a single service, no pilot pattern needed). Each phase corresponds to a tier in `TIER_RUBRIC.md`:

| Phase | Target | Wall-clock | Notes |
|---|---|---|---|
| **Phase 1** ✓ | C+ | done | mypy + SAST + perf baseline + CHANGELOG |
| **Phase 2** | A | 4-6 sessions (~3 wk calendar) | Coverage → 85%, image hardening, full obs stack, SLOs, runbooks |
| **Phase 3** | A+ | 3-4 sessions (~2 wk) | ADRs, OTel traces, Renovate, SBOM, cosign, chaos drills |
| **Phase 4** | S | 4-6 sessions (~3 wk) | Property + mutation tests, SLSA L2/L3, distroless, circuit breakers, DR |
| **Phase 5** | S+ | 2-3 sessions + 90-day soak | Single-command audit, spec-driven workflow, calendar gates aged |

**Total estimated effort:** 13-19 sessions plus 90 days of calendar-bound soak (mutation kill rates, quarterly secret rotation, runbook-tested-from-scratch validations). The work itself fits in 6-9 calendar weeks at a sustainable cadence; **S+ certification** lands 90 days after Phase 5 begins.

**Pilot strategy:** none. Klukai is one FastAPI service + one Flutter PWA + one voice service. The dimensions are the slices.

---

## 2. Goals

1. Lift klukai from **C+ tier** (post-Phase 1) to **S+ tier** per `~/.claude/TIER_RUBRIC.md`.
2. Ship a **single-command audit** (`scripts/s-tier-audit.sh`) that returns 0 when all S+ criteria pass, with a diff against last-known-green.
3. Preserve klukai's character integrity: NO regression on chat memory, affection state, voice quality, or persona. Per global CLAUDE.md absolute directive *"NEVER Delete Chat Memories"*, the test suite expansion never wipes user data.
4. Zero downtime during uplift. companion-core stays up; rollout via Docker compose update + healthcheck gating.

## 3. Non-Goals

- **No multi-region.** klukai is amarillo (core) + dominus (voice/GPU). Single failure domain accepted; DR via offsite tar to dominus.
- **No K8s.** Docker compose stays — `docker-compose.yml` + `gateway/docker-compose.yml` + `docker-compose.voice.yml`.
- **No public auth / OAuth.** Bearer tokens via seed users continue. Cloudflare in front for TLS.
- **No service split.** companion-core stays a single FastAPI app despite the >500-LOC files (those get refactored, not extracted into microservices).
- **No model swap.** dolphin-24b + gpt-oss-20b + gemma-4 (annotation/selection/quick) topology stays per `feedback_dolphin_for_annotations.md`.
- **No macOS/darwin builds** (global CLAUDE.md absolute).
- **No `actions/attest-build-provenance`** (global CLAUDE.md absolute — requires paid plan).
- **No flutter rewrite.** flutter_app stays as the PWA shell. The build pipeline gets hardened; the code stays.
- **No password rotation as part of this uplift** — global CLAUDE.md `feedback_no_password_changes.md` overrides any audit-driven rotation suggestion.

---

## 4. Locked Decisions

The decision matrix below captures the *why* at decision-time so future archaeology is unnecessary. Each row gets a corresponding ADR when Phase 3 lands.

| # | Question | Decision | Implication |
|---|---|---|---|
| Q1 | Tier ambition | Full S+ across all 8 dimensions | Long plan, calendar-bound gates |
| Q2 | Topology | amarillo (core, gateway) + dominus (voice, ComfyUI, LM Studio) | DR by offsite tar; no clustering |
| Q3 | Observability stack | Grafana Alloy → Prom + Loki + Tempo + Grafana, OTLP-emitting `companion-core` | Same shape as mcpservers stack; reuse if possible |
| Q4 | Secrets management | systemd-creds TPM-sealed on amarillo + `.env` for dev; SOPS+age as stretch | `seed_password_*` env vars graduate from `.env` to credstore |
| Q5 | Testing depth | Unit + integration via testcontainers + golden on prompts + property on parsers + mutation on `app/affection.py` + `app/audit_chain.py` + perf gate via existing probe | ≥95% coverage on `app/`; mutation kill ≥80% on shared paths |
| Q6 | Versioning | SemVer with `v0.x.y` until Phase 5 lands, then `v1.0.0` | `docker-compose.yml` images get explicit tags; no `:latest` |
| Q7 | Supply chain | trivy + grype + syft + cosign keyless + bandit + safety + gitleaks + CodeQL on PR; SLSA L2 via `slsa-github-generator` | Heavy CI; PR pipeline budget < 8 min |
| Q8 | Auth model | Bearer tokens + per-user rate-limit; admin role for audit endpoints | No OAuth; existing `app/auth.py` extended |
| Q9 | Docs model | MkDocs Material + auto-tool-ref + ADRs + dashboards-as-code + alert catalog + runbook anchors | `docs/` becomes the operator portal at `klukai.appnest.cc/docs/` |
| Q10 | Performance gate | Per-endpoint SLOs from `docs/perf-baseline.md` + PR gate (>20% p99 delta = fail vs `main` baseline) | Probe at `tools/load-test/probe.py` extended |
| Q11 | Character regression gate | Golden test suite over 50 canonical prompts; affection level transitions + speech-pattern routing covered | Catches the kind of bug fixed in `feedback_speech_routing_bug.md` |
| Q12 | Memory integrity gate | Read-only audit: `scripts/audit-memories.sh` counts Qdrant points, PG memory rows, Redis sessions; alerts on >5% drop | Per `feedback_never_delete_chat.md` absolute directive |

---

## 5. Architecture changes

### 5.1 Repo layout (final state)

```
klukai/
├── docker-compose.yml                  # core + gateway (amarillo)
├── docker-compose.voice.yml            # voice + ComfyUI (dominus)
├── docker-compose.obs.yml              # NEW: alloy + prom + loki + tempo + grafana
├── docker/
│   └── core/
│       ├── Dockerfile                  # multi-stage, distroless final
│       ├── app/
│       │   ├── routes/                 # split from routes.py
│       │   │   ├── __init__.py
│       │   │   ├── chat.py            (~150 LOC)
│       │   │   ├── memory.py          (~200 LOC)
│       │   │   ├── audit.py           (~150 LOC)
│       │   │   ├── admin.py           (~150 LOC)
│       │   │   ├── user.py            (~200 LOC)
│       │   │   └── health.py          (~80 LOC)
│       │   ├── proactive/              # split from proactive.py
│       │   │   ├── __init__.py
│       │   │   ├── scheduler.py
│       │   │   ├── missions.py
│       │   │   ├── messages.py
│       │   │   └── reflections.py
│       │   ├── personality/            # split from personality.py
│       │   │   ├── __init__.py
│       │   │   ├── speech_patterns.py
│       │   │   ├── moods.py
│       │   │   └── system_prompt.py
│       │   ├── observability/          # NEW
│       │   │   ├── tracing.py         (OTel setup)
│       │   │   ├── metrics.py         (Prom scrape)
│       │   │   └── logging.py         (structured + trace_id)
│       │   ├── circuit_breakers.py     # NEW: per-dep CB
│       │   └── ... (existing)
│       ├── tests/
│       │   ├── unit/                   # existing 46 files
│       │   ├── integration/            # NEW: testcontainers
│       │   ├── contract/               # NEW: JSON-schema validation
│       │   ├── property/               # NEW: hypothesis
│       │   ├── golden/                 # NEW: prompt snapshots
│       │   └── perf/                   # NEW: probe.py extended
│       ├── mypy.ini                    # tightening through phases
│       ├── ruff.toml                   # tightening through phases
│       └── pyproject.toml              # NEW: replaces requirements.txt with SHA pins
├── docs/
│   ├── adr/                            # NEW: 0001..0020 decisions
│   │   ├── 0001-s-plus-uplift.md
│   │   ├── 0002-amarillo-dominus-split.md
│   │   ├── 0003-three-tier-memory.md
│   │   └── ...
│   ├── runbooks/                       # NEW: top-10 alerts
│   │   ├── db-down.md
│   │   ├── voice-unreachable.md
│   │   ├── lm-studio-cold.md
│   │   ├── qdrant-down.md
│   │   ├── high-latency.md
│   │   └── ...
│   ├── slos.md                         # NEW: per-endpoint SLO definitions
│   ├── architecture.md                 # NEW: lifted from README
│   ├── perf-baseline.md                # EXISTING
│   ├── dashboards/                     # NEW: Grafana JSON
│   │   ├── overview.json
│   │   ├── chat-path.json
│   │   ├── memory-pipeline.json
│   │   └── voice-image-gen.json
│   ├── alerts/                         # NEW: Prom alert YAML
│   │   ├── klukai.yaml                 # all klukai-specific alerts
│   │   └── infra.yaml                  # shared infra (PG, Redis, Qdrant)
│   └── superpowers/                    # EXISTING
├── scripts/
│   ├── s-tier-audit.sh                 # NEW: single-command audit
│   ├── restore-from-backup.sh          # NEW: tested-restore drill
│   ├── audit-memories.sh               # NEW: memory integrity check
│   ├── chaos-kill-dep.sh               # NEW: chaos drill harness
│   └── ... (existing)
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                      # EXTENDED: add trivy, grype, codeql, sbom
│   │   ├── nightly.yml                 # NEW: mutation tests, perf collection
│   │   ├── release.yml                 # NEW: cosign + slsa
│   │   └── dependabot-or-renovate.yml  # NEW
│   ├── CODEOWNERS                      # NEW
│   ├── PULL_REQUEST_TEMPLATE.md        # NEW
│   ├── ISSUE_TEMPLATE/                  # NEW
│   │   ├── bug-report.md
│   │   ├── runbook-incident.md
│   │   └── feature-request.md
│   └── renovate.json                   # NEW
└── ... (existing)
```

### 5.2 Observability stack (`docker-compose.obs.yml`)

Reuse the mcpservers stack pattern: Alloy collector → Prom (15d) + Loki (30d) + Tempo (7d) + Grafana.

| Container | Bind | Purpose |
|---|---|---|
| alloy | 127.0.0.1:12345, 4317, 4318 | OTLP collector, fans to Prom/Loki/Tempo |
| prometheus | 127.0.0.1:9090 | Metrics TSDB |
| loki | 127.0.0.1:3100 | Log aggregator |
| tempo | 127.0.0.1:3200 | Trace store |
| grafana | 127.0.0.1:3000 | UI; provisioned datasources + dashboards |

All bound to loopback; access via Tailscale + Cloudflare gateway. Volumes under `/mnt/nvmeINT/obs/klukai/`. companion-core emits OTLP traces + metrics + structured logs (correlation via `trace_id`).

**Decision:** reuse mcpservers' obs stack if it's already running on amarillo. If port conflicts, klukai uses adjacent ports (`+1000`).

### 5.3 Secrets — systemd-creds + TPM-seal

Current state: `.env` contains `POSTGRES_PASSWORD`, `SEED_PASSWORD_*`, optional `ANTHROPIC_API_KEY`, `VAPID_*`. File-mode 600, not committed.

Target state:
- `.env` deprecated for production; remains for dev only.
- Production secrets in `/etc/credstore.encrypted/klukai-secrets.cred`, TPM2-sealed.
- `docker-compose.yml` reads via `LoadCredential=` in the unit wrapper that starts compose.
- Rotation: re-seal → restart. Git log shows changed-when, never changed-to.

Stretch goal (Phase 4): SOPS + age for in-repo encrypted secret bundles to simplify multi-host (dominus) rotation. Not required for S+ minimum.

### 5.4 Circuit breakers

`app/circuit_breakers.py` wraps every external dependency:

| Dep | Open threshold | Half-open probe | Fallback |
|---|---|---|---|
| PostgreSQL | 5 errs / 10s | 1 query / 30s | Return cached state from Redis |
| Redis | 5 errs / 10s | 1 PING / 15s | Disable session caching; pass-through to PG |
| Qdrant | 3 errs / 30s | 1 search / 60s | Empty episodic memory; fall back to PG memories |
| LM Studio | 3 errs / 60s | 1 health / 120s | Anthropic fallback (if `ANTHROPIC_API_KEY` set) or 503 |
| voice (dominus) | 5 errs / 60s | 1 health / 120s | Skip TTS; deliver text-only |
| ComfyUI | 3 errs / 60s | 1 health / 300s | Skip image gen; surface "image unavailable" |

Implementation: standard token-bucket + state-machine; emit OTel span attributes on state transitions; metrics: `klukai_circuit_state{dep="postgres"}`.

### 5.5 Test cake (seven layers)

Per `TIER_RUBRIC.md` S-tier: "property-based + mutation + golden + integration + contract + perf + unit."

| Layer | Tool | Where | Trigger |
|---|---|---|---|
| Unit | pytest | `tests/unit/` (existing 46 files) | every PR |
| Contract | pytest + JSON Schema | `tests/contract/` | every PR |
| Property | hypothesis | `tests/property/` | every PR |
| Golden | pytest + snapshot | `tests/golden/` | every PR; manual rotate |
| Integration | testcontainers (PG, Redis, Qdrant) | `tests/integration/` | every PR |
| Performance | `tools/load-test/probe.py` + budget gate | `tests/perf/` | every PR; 20% p99 delta = fail |
| Mutation | mutmut | `tests/unit/` + `app/affection.py` + `app/audit_chain.py` | nightly only; ≥80% kill score |

Coverage gate: `--cov-fail-under` ratchets through phases:
- Phase 1: 49% (current)
- Phase 2: 70%
- Phase 3: 85%
- Phase 4: 95% on `app/` (lower bar for `app/main.py` startup glue)
- Phase 5: 95% maintained + mutation gate enforced

### 5.6 Image hardening

Current `docker/core/Dockerfile`:
- `python:3.13-slim` base (~125MB)
- Non-root `appuser` ✓
- Single-stage build

Target Phase 2-3:
- Multi-stage: `python:3.13-slim` as builder → `gcr.io/distroless/python3-debian12` as runtime (~50MB)
- Pin all packages to SHA via `uv` or `pip-compile --generate-hashes`
- Inline healthcheck in Dockerfile (matches compose healthcheck)
- Build-time `--mount=type=cache` for pip
- Reproducible build: `SOURCE_DATE_EPOCH` from `git log -1 --format=%ct`

Target Phase 4:
- cosign keyless signing on push (`COSIGN_EXPERIMENTAL=1`)
- SBOM via syft (CycloneDX + SPDX), attested via cosign
- SLSA L2 provenance via `slsa-framework/slsa-github-generator/.github/workflows/generator_container_slsa3.yml`
- Image scan via trivy + grype on PR; HIGH/CRITICAL = fail

### 5.7 Character integrity tests

Klukai's character is the product. Tier rubric doesn't have a "doesn't break the personality" criterion, but losing voice consistency or memory permanence is worse than any test failure. Per global CLAUDE.md absolute directives:
- "NEVER Delete Chat Memories" — chat messages, episodes, affection, Qdrant vectors are SACRED.
- "Commander is HUMAN" — never a T-Doll.
- "Speech Pattern Routing Bug" — levels 5-9 default-to-Cold bug must never reoccur.

Golden test suite in `tests/golden/` snapshots:
- 50 canonical prompts × 10 affection levels × 6 mood categories = 3000 outputs.
- Drift = test fail; intentional rotation requires manual `--update-snapshots` flag.
- Auto-comparison flags semantic shift (BLEU / cosine similarity against baseline).

Memory integrity tests in `tests/integration/test_memory_integrity.py`:
- Insert chat → assert PG row.
- Run compaction → assert NO row deleted (only summarized).
- Run vacuum → assert Qdrant points preserved.
- Migration roundtrip → assert affection state intact.

### 5.8 Single-command audit (`scripts/s-tier-audit.sh`)

The exit-criterion for S+. Mirrors the mcpservers acceptance test pattern:

```bash
#!/usr/bin/env bash
# scripts/s-tier-audit.sh — single-command S+ audit for klukai.
# Exit 0 = all S+ criteria pass; exit 1 = at least one fail.

set -euo pipefail
RESULTS=()

check() {
  local name="$1" cmd="$2"
  if eval "$cmd"; then
    RESULTS+=("✓ $name")
  else
    RESULTS+=("✗ $name")
  fi
}

# Code quality
check "ruff strict"           "ruff check docker/core/app/ --select=E,F,W,B,UP,SIM,RUF"
check "mypy strict"           "mypy docker/core/app/ --strict"
check "no files >500 LOC"     "! find docker/core/app -name '*.py' -exec wc -l {} + | awk '\$1 > 500' | grep -q ."
check "complexity <10"        "radon cc docker/core/app/ -a -nb"

# Testing
check "coverage ≥95%"         "cd docker/core && pytest --cov=app --cov-fail-under=95 -q"
check "mutation kill ≥80%"    "mutmut results | grep -E 'killed.*≥80%'"
check "golden tests pass"     "cd docker/core && pytest tests/golden/ -q"
check "contract tests pass"   "cd docker/core && pytest tests/contract/ -q"
check "integration tests pass" "cd docker/core && pytest tests/integration/ -q"

# Security
check "bandit clean"          "bandit -r docker/core/app -ll"
check "safety clean"          "safety check --file docker/core/requirements.txt"
check "trivy image clean"     "trivy image --severity HIGH,CRITICAL klukai:latest"
check "no plaintext secrets"  "gitleaks detect --no-banner"
check "SBOM exists"           "test -f sbom.cdx.json"
check "image signed"          "cosign verify klukai:latest --certificate-identity-regexp='.*'"

# Reliability
check "DR drill < 6h old"     "test \$(find /mnt/nvmeINT/backups/dr-drill -mtime -1 | wc -l) -gt 0"
check "circuit breakers wired" "grep -q CircuitBreaker docker/core/app/circuit_breakers.py"
check "graceful shutdown"     "grep -q 'shutdown_event' docker/core/app/main.py"

# Observability
check "OTel traces emitted"   "curl -sf http://localhost:3000/api/datasources/proxy/3/api/search?service=klukai | jq '.data | length > 0'"
check "RED metrics per endpt" "curl -sf http://localhost:9090/api/v1/query?query=http_requests_total{job=\"klukai\"} | jq '.data.result | length > 5'"
check "dashboards in repo"    "test -d docs/dashboards && ls docs/dashboards/*.json | head -1"
check "alerts link runbooks"  "yq '.groups[].rules[].annotations.runbook_url' docs/alerts/klukai.yaml | grep -v null"

# Performance
check "SLO doc exists"        "test -f docs/slos.md"
check "perf gate active"      "grep -q 'p99_delta_threshold' .github/workflows/ci.yml"
check "no >20% p99 regression" "python3 tools/load-test/probe.py --baseline docs/perf-baseline.json"

# Documentation
check "ADRs ≥10"              "ls docs/adr/*.md | wc -l | awk '\$1 >= 10'"
check "runbooks ≥10"          "ls docs/runbooks/*.md | wc -l | awk '\$1 >= 10'"
check "CHANGELOG current"     "grep -q \"\$(date +%Y-%m)\" CHANGELOG.md"
check "onboarding tested"     "test -f docs/onboarding-test-result.json"

# Process
check "CODEOWNERS exists"     "test -f .github/CODEOWNERS"
check "PR template exists"    "test -f .github/PULL_REQUEST_TEMPLATE.md"
check "renovate configured"   "test -f renovate.json -o -f .github/renovate.json"
check "release.yml signs"     "grep -q cosign .github/workflows/release.yml"

# Calendar gates
check "secret rotated <90d"   "test \$(stat -c %Y /etc/credstore.encrypted/klukai-secrets.cred) -gt \$(date -d '90 days ago' +%s)"
check "runbook tested <90d"   "find docs/runbooks/ -name '*.md' -newermt '90 days ago' | head -1"

# Verdict
printf '%s\n' "${RESULTS[@]}"
PASS=$(printf '%s\n' "${RESULTS[@]}" | grep -c '^✓')
TOTAL=${#RESULTS[@]}
echo
echo "Score: $PASS / $TOTAL"
[[ $PASS -eq $TOTAL ]] || exit 1
```

A green run is the definition of S+. Until every check is ✓, klukai isn't S+ — it's whatever the floor says.

---

## 6. Phase 2 — Lift to A

**Target:** A tier per `TIER_RUBRIC.md`. Exit criterion: every A-tier row holds.

**Scope (4-6 sessions):**

### 6.1 Code structure (1 session)

Split the three monsters:

- `app/proactive.py` (1628 LOC) → `app/proactive/` package with `scheduler.py`, `missions.py`, `messages.py`, `reflections.py`. Public API stays at `app.proactive` via `__init__.py` re-exports.
- `app/routes.py` (1091 LOC) → `app/routes/` package by endpoint group (chat, memory, audit, admin, user, health). FastAPI routers merged in `app.routes:register_all(app)`.
- `app/personality.py` (797 LOC) → `app/personality/` package with `speech_patterns.py`, `moods.py`, `system_prompt.py`.

Each file < 500 LOC. No behavior change. Test suite re-runs after each split.

### 6.2 Coverage 49% → 85% (2 sessions)

- testcontainers for PG, Redis, Qdrant in `tests/integration/conftest.py`.
- Replace ~30 of the heavily-mocked `tests/test_user_api.py` failures with real integration tests against testcontainers.
- New test files (target):
  - `tests/integration/test_chat_e2e.py` — full chat round-trip with PG+Redis+Qdrant.
  - `tests/integration/test_memory_pipeline.py` — episodic → semantic → factual.
  - `tests/integration/test_audit_chain.py` — sign → verify → tamper-detect.
  - `tests/integration/test_affection_progression.py` — level 0 → 9 transitions.
- Ratchet `--cov-fail-under` to 85% in `ci.yml`.

### 6.3 Image hardening (1 session)

- Multi-stage Dockerfile + distroless final layer.
- `uv pip compile --generate-hashes requirements.in > requirements.txt` for SHA-pinned deps.
- Add `trivy` and `grype` to CI; HIGH+CRITICAL fail.
- Add `syft` SBOM gen as informational (Phase 3 wires the gate).

### 6.4 Subsystem health cache (0.5 session)

Per `docs/perf-baseline.md` Phase 2 target: `/health` p99 ≤ 30ms.

- New module `app/observability/health_cache.py` with 5s TTL cache.
- `/health` reads from cache; cache refreshed by 1 Hz background task.
- Add `/api/health/live` (no backend deps) + `/api/health/ready` (full check) per K8s convention.

### 6.5 Observability stack online (1 session)

- `docker-compose.obs.yml` — alloy + prom + loki + tempo + grafana.
- companion-core: OTel SDK init, span on every route, metrics counter per endpoint.
- structured logging: add `trace_id`, `request_id`, `user_id`, `affection_level` to every log line.
- Provision 4 Grafana dashboards: overview, chat-path, memory-pipeline, voice+image-gen.
- 10 Prom alerts in `docs/alerts/klukai.yaml`; each with `runbook_url` annotation.

### 6.6 SLO definition (0.5 session)

`docs/slos.md` codifies:

| Endpoint | Latency target | Availability | Notes |
|---|---|---|---|
| `/health` | p99 ≤ 30ms | 99.95% | Loopback, cached |
| `/api/chat/*` | p99 ≤ 8s | 99.9% | Dominated by LM Studio |
| `/api/memories/search` | p99 ≤ 500ms | 99.9% | Qdrant |
| `/api/user/stats` | p99 ≤ 200ms | 99.9% | PG read |
| `/api/audit/log` | p99 ≤ 100ms | 99.95% | PG write |
| WebSocket `/ws` | conn time p99 ≤ 200ms | 99.9% | Reconnect-tolerant |

Error budget = 1 - SLO. Burn rate alert when 14d budget consumed in < 24h.

### 6.7 Runbooks (1 session)

Top-10 alerts each get a markdown runbook in `docs/runbooks/`:

1. `db-down.md` — PG pool exhausted or connection refused.
2. `redis-down.md` — Redis unreachable; sessions lost.
3. `qdrant-down.md` — vector recall failing; semantic memory disabled.
4. `lm-studio-cold.md` — first request >30s; gemma idle-unloaded.
5. `voice-unreachable.md` — dominus TTS endpoint failing.
6. `comfyui-down.md` — image gen failing.
7. `high-latency.md` — p99 SLO breach across endpoints.
8. `disk-space.md` — `/mnt/nvmeINT` low.
9. `memory-leak.md` — RSS growth > 50%/24h on companion-core.
10. `auth-fail-spike.md` — bearer rejection rate > 5%.

Each runbook: symptom, severity, immediate action, root-cause investigation, post-incident notes template.

### 6.8 Tested restore (0.5 session)

`scripts/restore-from-backup.sh`:
1. Pull latest offsite tar from dominus.
2. Spin a scratch PG container.
3. Restore dump.
4. Run schema validation + row-count sanity (>0 messages, >0 memories, etc.).
5. Run `tests/integration/test_restore_health.py` against the restore.
6. Emit JSON report; exit 0 on green.

Scheduled monthly via systemd timer on amarillo. Pass = pass; fail = page.

**Phase 2 exit criteria:** every row of the A-tier rubric column holds. Code quality, testing, security, reliability, observability, performance, documentation, process all ≥ A. `scripts/s-tier-audit.sh` Phase-2 subset returns green.

---

## 7. Phase 3 — Lift to A+

**Target:** A+ tier per `TIER_RUBRIC.md`. Exit criterion: every A+ row holds.

**Scope (3-4 sessions):**

### 7.1 ADRs for every load-bearing decision (1 session)

Write 15-20 ADRs in `docs/adr/`, one per major decision (or decision cluster) already taken:

| ADR | Topic | Source |
|---|---|---|
| 0001 | S+ uplift plan | This doc |
| 0002 | amarillo/dominus split | Current architecture |
| 0003 | Three-tier memory (Redis→Qdrant→PG) | `feedback_never_delete_chat.md` |
| 0004 | LM Studio routing (gemma/dolphin/gpt-oss) | `feedback_dolphin_for_annotations.md` + `feedback_model_routing.md` |
| 0005 | Affection level taxonomy (0-9 + speech patterns) | `app/personality.py` history |
| 0006 | Image gen pipeline (Illustrious + Klukai LoRA) | `reference_illustrious.md` |
| 0007 | Voice on dominus only (RTX 3090 + CUDA) | `feedback_dominus_voice_port.md` |
| 0008 | Audit chain HMAC tamper-detection | `feat(security): wire audit chain hashing` |
| 0009 | Cloudflare in front, nginx gateway behind | `feedback_cloudflare_cache.md` |
| 0010 | Flutter PWA with --base-href=/app/ | `feedback_flutter_base_href.md` |
| 0011 | Klukai is a T-Doll; Commander is HUMAN | `feedback_commander_human.md` |
| 0012 | Memory seeding cadence (every 2 days 3-6 AM) | `feedback_memory_seeding_schedule.md` |
| 0013 | Klukai vs Kairi separation | `feedback_klukai_kairi_separate.md` |
| 0014 | Off-site backup amarillo → dominus | `feat(ops): off-site backup` |
| 0015 | wsl2 decommissioned | `feedback_wsl2_decommissioned.md` |

Each ADR follows MADR-lite format: context, decision, status, consequences, links.

### 7.2 Renovate + dep updates (0.5 session)

`.github/renovate.json` covering:
- Python deps (group dev/prod separately).
- Dockerfile base images.
- GHA actions (SHA-pinned per Q7).
- Auto-merge for patch + security; manual for minor/major.

### 7.3 SBOM + cosign signing (0.5 session)

- `.github/workflows/release.yml`: build → syft → cosign sign → cosign attest sbom.
- SBOM uploaded as release asset.
- README badge: signed-by-cosign + sbom-attested.

### 7.4 Dashboards-as-code review (0.5 session)

- `docs/dashboards/*.json` reviewed in PR — Grafana writes don't escape into the live instance without PR review.
- `grizzly` or `jsonnet` to deduplicate dashboard JSON (stretch, may stay vanilla JSON).

### 7.5 Chaos drills (0.5 session)

`scripts/chaos-kill-dep.sh <dep>`:
- Kills the named dep (PG, Redis, Qdrant, voice, LM Studio).
- Holds for 30-60s.
- Restores.
- Captures Grafana screenshot of impact + recovery time.
- Stores under `docs/chaos-drills/<date>-<dep>.md`.

Run quarterly per dep. Goal: confirm circuit breakers actually kick in.

### 7.6 Auto-changelog via git-cliff (0.5 session)

- `cliff.toml` configured to group by conventional-commit type.
- `.github/workflows/release.yml` regenerates CHANGELOG.md on tag.
- Existing manual CHANGELOG.md becomes the baseline; future entries auto-append.

**Phase 3 exit criteria:** A+ tier rubric holds. ADRs ≥ 15. Renovate active. Dashboards in repo. First chaos drill report committed.

---

## 8. Phase 4 — Lift to S

**Target:** S tier per `TIER_RUBRIC.md`. Exit: every S row holds.

**Scope (4-6 sessions):**

### 8.1 Property + golden tests (1.5 sessions)

- Hypothesis on parsers: `app/llm_json.py`, `app/helpers.py` detectors, `app/personality/speech_patterns.py`.
- Golden tests on `app/personality/system_prompt.py` outputs across 30 canonical (affection, mood, time_of_day) tuples.
- Snapshot rotation: `pytest --update-snapshots` flag, with `pre-commit` hook flagging mass rotations for review.

### 8.2 Mutation testing (1 session)

- `mutmut` on `app/affection.py` + `app/audit_chain.py` + `app/personality/speech_patterns.py` (the core character paths).
- Nightly GHA workflow runs full mutation; PR builds skip it.
- Kill score ≥ 80% gate (after one week of soak to baseline).

### 8.3 Circuit breakers (1 session)

Implement `app/circuit_breakers.py` per §5.4. Wire every external dep call through it. Emit OTel + Prom metrics on state transitions.

### 8.4 Distroless + SLSA L2 (1 session)

- Dockerfile final stage → `gcr.io/distroless/python3-debian12:nonroot`.
- `.github/workflows/release.yml` invokes `slsa-framework/slsa-github-generator/.github/workflows/generator_container_slsa3.yml` (yields L2 due to free-org plan constraints; L3 needs paid runners).
- Banned `actions/attest-build-provenance` explicitly NOT used.

### 8.5 RED metrics per endpoint (0.5 session)

- `app/observability/metrics.py` extended: every route emits Rate + Errors + Duration histograms.
- Grafana dashboard: per-endpoint RED panel.

### 8.6 SLO error budgets in code (0.5 session)

- `docs/slos.md` rendered as `app/observability/slos.py` constants.
- Burn-rate alert generates from SLO config — single source of truth.

### 8.7 Single-command DR (0.5 session)

`scripts/disaster-recovery.sh`:
1. Stops klukai compose stack on amarillo.
2. Pulls latest tar from dominus.
3. Restores PG dump.
4. Restores Qdrant collections.
5. Restores `companion-images` volume.
6. Restarts stack.
7. Runs smoke tests.
8. Reports time-to-recovery.

Test quarterly. Target RTO < 30 min.

**Phase 4 exit criteria:** S tier rubric holds. Mutation kill ≥ 80% sustained. Distroless + SLSA L2 active. Circuit breakers verified via chaos drill.

---

## 9. Phase 5 — Lift to S+

**Target:** S+ tier per `TIER_RUBRIC.md`. Exit: every S+ row holds AND `scripts/s-tier-audit.sh` returns green AND 90-day calendar soak passed.

**Scope (2-3 sessions + 90-day soak):**

### 9.1 100% coverage on shared/core libs (0.5 session)

- `app/affection.py`, `app/audit_chain.py`, `app/personality/`, `app/helpers.py` all hit 100% line + branch coverage.
- Per-file gate in `mypy.ini` and `pyproject.toml`.

### 9.2 ADRs for every Q&A decision (0.5 session)

Backfill ADRs for every locked decision in this doc's §4 table. One ADR per Q. Total ADR count ≥ 25.

### 9.3 Onboarding doc tested-from-scratch (1 session)

- `docs/onboarding.md` — fresh-laptop walkthrough to running `klukai` locally.
- Quarterly drill: a fresh user (or fresh VM) follows the doc, reports any drift, doc is updated.
- `docs/onboarding-test-result.json` records last drill date + outcome.

### 9.4 External-audit framework mapping (0.5 session)

- `docs/audit-mapping.md` — maps klukai controls to SOC2-lite checklist (klukai isn't enterprise SaaS so full SOC2 isn't useful, but the mapping shows audit-readiness).

### 9.5 Single-command audit (0.5 session)

`scripts/s-tier-audit.sh` per §5.8 with every check wired.

### 9.6 Calendar gates (90-day soak)

These cannot be completed in-session; they age in over time:

- **Mutation kill ≥ 80% sustained over 4 weeks** (one full nightly cycle of 30 days passing).
- **Secret rotation tested ≥ once** (rotate `POSTGRES_PASSWORD` per the rotation runbook; verify zero downtime).
- **Quarterly onboarding drill passed once** (someone other than the author runs `docs/onboarding.md` to a working state).
- **One real production incident resolved using a runbook** (proves the runbook isn't theatre).
- **Chaos drill quarterly passes for all 6 deps** (one per fortnight is OK for the soak window).

**Phase 5 exit criteria:** `scripts/s-tier-audit.sh` returns 0. CHANGELOG.md has a `v1.0.0` entry. ADR-0001 status flipped from Draft to Accepted.

---

## 10. Risks & plan health

### 10.1 Strategic risks (could kill the project)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **Character regression in test rewrite** | Medium | High | Golden test suite per §5.7; mutation testing on `app/affection.py` + `app/personality/`; review every change against `feedback_speech_routing_bug.md` pattern |
| **Memory loss during testcontainers integration** | Low | Catastrophic | `feedback_never_delete_chat.md` absolute rule; integration tests run against isolated test DB, never touch `companion_*` tables in prod |
| **dominus voice degradation during obs rollout** | Low | Medium | dominus voice container untouched in Phase 2-3; Phase 4 circuit breaker work proves voice resilience |
| **CI runtime blows up (>15 min)** | Medium | Medium | Job parallelism + matrix splits; cache pip + docker layers; nightly mutation runs separately |
| **TPM-sealing fails on amarillo** | Low | Medium | Fall back to file-mode-600 `.env` + Phase 4 SOPS+age path |

### 10.2 Tactical risks (could slow the project)

| Risk | Mitigation |
|---|---|
| File-split breaks imports | Each split commit runs full test suite; cherry-pick revert if regressions appear |
| testcontainers slow on amarillo | Use ramdisk volume for test PG; cache image |
| Renovate noise on first run | Phase 3 tunes to weekly batch; auto-merge security only |
| Dashboards drift from code | PR review on `docs/dashboards/*.json`; nightly job exports live → diff |

### 10.3 Plan health monitor

The plan itself is observable. Per the mcpservers reference pattern:

- This doc's `Status:` field tracks progression: Draft → Approved → In-Progress → Phase-N-Complete → Done.
- Each phase has a tracking issue in GitHub: `[Phase 2]` `[Phase 3]` etc., with checkbox-per-task lists.
- Weekly retro entry in `docs/plan-health/<date>.md` answers: what landed, what's blocked, what's reframed.

### 10.4 Decision-reversal protocol

If a locked decision needs reversal (e.g., Q3 obs stack swap):
1. Open a new ADR `0NNN-reverse-QX.md` referencing this doc's §4.
2. State the new evidence + new decision.
3. Update this doc's `Supersedes` chain.
4. Update `scripts/s-tier-audit.sh` if criteria shift.

### 10.5 Out-of-scope (named so they don't sneak in)

- Multi-tenant SaaS hardening (klukai is private-Tailnet + 4 seed users).
- Mobile native apps (Flutter PWA is the only client).
- Voice cloning of additional characters (Klukai is the only product).
- LLM fine-tuning (using off-the-shelf models per `feedback_local_llm.md`).
- Federation with other companion AIs (klukai is standalone).

---

## 11. Success criteria

### 11.1 Measurable definition of S+

Every check in `scripts/s-tier-audit.sh` returns ✓ AND has held continuously for 90 days. The 90-day soak window is what separates "claims S+" from "is S+."

### 11.2 Continuous quality gates (post-S+)

After `v1.0.0`, the following stay green or main is blocked:
- PR gate: lint + typecheck + unit + contract + property + integration + perf-delta.
- Nightly: mutation + full perf collection + chaos drill rotation.
- Weekly: dashboard drift check + dep update batch.
- Monthly: restore drill.
- Quarterly: onboarding drill + audit-mapping refresh + secret rotation.

### 11.3 External-audit readiness (free bonus)

The work above plus `docs/audit-mapping.md` makes klukai sufficiently structured to survive:
- Personal-data privacy audit (memory storage + retention).
- Source-code security audit (SAST + dep scan + secrets).
- Operational maturity audit (runbooks + SLOs + post-incident records).

Not audit-pass-guaranteed; audit-defensible.

### 11.4 Stretch goals (NOT in this plan)

These appear in mcpservers' S+ but klukai doesn't need them:
- Distributed tracing across multi-region (klukai is single-region).
- Compliance certification (SOC2, ISO27001) — out of scope for a personal product.
- API SDK generation (klukai has no external API consumers).

---

## 12. Appendix — File paths added

```
docs/superpowers/specs/2026-05-16-s-plus-uplift.md   THIS DOC
docs/adr/0001-s-tier-uplift.md                       Phase 3
docs/adr/0002..0020-*.md                              Phase 3
docs/runbooks/{db-down,redis-down,...}.md             Phase 2 (10 files)
docs/slos.md                                          Phase 2
docs/architecture.md                                  Phase 2
docs/dashboards/{overview,chat-path,memory-pipeline,voice-image-gen}.json  Phase 2
docs/alerts/{klukai,infra}.yaml                       Phase 2
docs/audit-mapping.md                                 Phase 5
docs/onboarding.md                                    Phase 5
docs/onboarding-test-result.json                      Phase 5
docs/plan-health/<date>.md                            ongoing
docs/chaos-drills/<date>-<dep>.md                     Phase 3+
docker/core/Dockerfile                                Phase 2 (multi-stage rewrite)
docker/core/pyproject.toml                            Phase 2 (replaces requirements.txt)
docker/core/app/routes/{__init__,chat,memory,audit,admin,user,health}.py  Phase 2
docker/core/app/proactive/{__init__,scheduler,missions,messages,reflections}.py  Phase 2
docker/core/app/personality/{__init__,speech_patterns,moods,system_prompt}.py  Phase 2
docker/core/app/observability/{tracing,metrics,logging,health_cache}.py  Phase 2
docker/core/app/circuit_breakers.py                   Phase 4
docker/core/tests/{integration,contract,property,golden,perf}/  Phase 2-4
docker-compose.obs.yml                                Phase 2
scripts/s-tier-audit.sh                               Phase 5
scripts/restore-from-backup.sh                        Phase 2
scripts/audit-memories.sh                             Phase 2
scripts/chaos-kill-dep.sh                             Phase 3
scripts/disaster-recovery.sh                          Phase 4
.github/workflows/{nightly,release}.yml               Phase 3-4
.github/CODEOWNERS                                    Phase 3
.github/PULL_REQUEST_TEMPLATE.md                      Phase 3
.github/ISSUE_TEMPLATE/{bug-report,runbook-incident,feature-request}.md  Phase 3
.github/renovate.json                                 Phase 3
cliff.toml                                            Phase 3
```

---

## 13. Approval

This document captures the **scope only**. Execution begins when the user
signals approval (e.g., "ok, start Phase 2"). Each phase ends with a
mini-retro in `docs/plan-health/<date>.md` and a re-affirmation that the
next phase is still wanted as-scoped.

The 90-day calendar soak in Phase 5 means klukai isn't S+ today, this
month, or this quarter — soonest realistic S+ certification date is
**2026-11-15** (6 months from now). That's not a slip; that's the
honest cost of the S+ floor's calendar gates.

Phase 1 (✓ done) lifted klukai from D-floor to C+. The remaining
phases are scoped, ordered, and ready to execute on approval.
