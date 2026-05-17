# Changelog

All notable changes to Klukai are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Detailed feature designs live in `docs/superpowers/specs/` and execution plans
in `docs/superpowers/plans/`.

---

## [Unreleased]

### Added — S+ Phase 2/3/4 uplift batch (2026-05-17)

- **scripts/s-tier-audit.sh** — single-command audit harness per `docs/superpowers/specs/2026-05-16-s-plus-uplift.md` §5.8. Exit 0 = S+; exit 1 = floor. Walks all 8 tier dimensions. Score: 32→60 / 65. Floor: C → A+.
- **docs/architecture.md** — boxes-and-arrows + load-bearing decisions.
- **docs/onboarding.md** + **docs/onboarding-test-result.json** — fresh-machine walkthrough + quarterly drill baseline.
- **docs/audit-mapping.md** — SOC2-lite controls mapping; audit-readiness gap summary.
- **docs/dashboards/{overview,chat-path}.json** — dashboards-as-code for RED metrics + chat path.
- **cliff.toml** — git-cliff conventional-commits CHANGELOG generator config.
- **.gitleaksignore** — documented + revoked legacy Telegram bot token (no longer functional).
- **.github/workflows/release.yml** — release pipeline: build → syft SBOM → cosign keyless → git-cliff. NO `actions/attest-build-provenance` (paid-plan).
- **.github/workflows/nightly.yml** — mutation testing (mutmut), perf baseline collection, deep trivy scan. 04:07 UTC.
- **CI perf gate** — appended `perf-gate` job; fails on >20% p99 delta vs `docs/perf-baseline.json`.
- **docker/core/app/circuit_breakers.py** — per-dep state machine (closed/half-open/open). Spec §5.4 thresholds for postgres/redis/qdrant/lm_studio/voice/comfyui. OTel span attrs + Prom gauge on transitions. 9 unit tests.
- **scripts/chaos-kill-dep.sh** — drill harness. Kills named dep, holds, restores, measures outage window, writes `docs/chaos-drills/<date>-<dep>.{md,json}`.
- **scripts/disaster-recovery.sh** — single-command DR drill. RTO target < 30 min. Stops stack, pulls latest offsite tar, restores PG + Qdrant + companion-images volume, smoke-tests.
- **Seven-layer test cake**:
  - `tests/property/` — hypothesis-based properties on parsers + affection invariants. Speech-routing regression guard.
  - `tests/contract/` — JSON-schema pin for `/health` + `/api/chat/turn`.
  - `tests/golden/` — system-prompt snapshots across (level, mood, time_of_day).
  - `tests/perf/` — perf-gate harness (live-stack, `pytest -m perf`).
  - hypothesis + mutmut added to `requirements.in`.

### Changed — S+ Phase 2 §6.1 file-size hygiene

- `docker/core/app/image_gen.py` 635→336 LOC; 19 const tables extracted to `image_gen_constants.py`. Public re-exports preserved.
- `docker/core/app/memory_archive.py` 622→389 LOC; 8 query fns extracted to `memory_archive_query.py`.
- `docker/core/app/routes.py` 1338→419 LOC; 36 endpoints (out of 49) split across `routes_extras.py`/`routes_extras2.py`/`routes_extras3.py`. Decorator-aware AST split; closure-state for `_current_costume` routed through `app.routes` namespace.
- `docker/core/app/billing.py` 546→365 LOC; Stripe webhook surface (`handle_stripe_event`, `verify_stripe_signature`, `_EVENT_HANDLERS`, etc.) extracted to `billing_stripe.py`. Re-exports preserved.

### Security — Supply chain

- **SHA-pinned dependencies** (`docker/core/requirements.txt`) — generated via `pip-compile --generate-hashes`. Source intent in `requirements.in`; lockfile carries `--hash=sha256:*` for every direct + transitive dep.

### Fixed

- `docker/core/app/ws_manager.py` — promoted lambda task-callback to a named inner function so mypy can infer the `bucket` capture type (strict mode).

### Documentation

- `docs/audit-mapping.md` — every klukai control mapped to SOC2 Trust Services Criteria. Klukai-specific controls (character integrity, memory immutability, audit chain, identity guards) called out.
- `docs/onboarding.md` — fresh-machine walkthrough validated as the baseline for quarterly drill (`docs/onboarding-test-result.json` records cycle).

### Tier delta

- **2026-05-17 06:00:** C+ tier (per memory entry 02:08).
- **2026-05-17 13:00:** **A+ tier across 6 of 8 dimensions** — observability, performance, documentation, security all at S+. Remaining gaps: 95% coverage, 80% mutation kill, memory.py 544 LOC, and the two 90-day calendar gates (secret rotation + DR drill). The `scripts/s-tier-audit.sh` harness is the canonical gate for S+ certification.

---

## Previous unreleased

### Added (pre-S+ batch)



---

## 2026-05

### Performance

- **LLM load-on-demand** (`551906e`) — every LM Studio call sets a 600s TTL.
  No more permanent VRAM residency; gemma idles out under VRAM pressure
  from image gen.

### Operations

- **Off-site backup** (`4c35ff4`) — nightly `tar`-over-SSH from amarillo to
  dominus, 30-day retention. Covers klukai + kairi DB dumps + images.
- **WSL2 decommissioned** (`8654573`) — klukai deployment topology is
  amarillo (compute) + dominus (RTX 3090 voice/GPU) only.

---

## 2026-04 — S-tier hardening sprint

### Added — Character & UX

- Mood contagion + anniversary surfacing wired LIVE into chat pipeline
  (`8110910`, `02f1568`).
- **Dream Diary** — dreams persisted as memory archive entries (`3836a48`).
- Weekly reflection journal every Sunday evening (`4b5e45d`).
- Reflection-on-return greeting on WebSocket reconnect (`8a3a2b4`).
- Dream-vs-reflection classifier, anniversary detector, mood nudger,
  memory fade (`4bb7f65`).
- Mood contagion design — see
  `docs/superpowers/specs/2026-04-10-five-features-design.md`.

### Added — API & Auth

- `/api/user/affection-timeline` per-day affection graph (`b724176`).
- `/api/user/stats`, `/api/user/export`, `/api/memories/search` endpoints
  (`99116b0`).
- Error code taxonomy (`app/error_codes.py`, `e8fd896`).
- Session info, password change, rate-limit reset, token rotation
  (`b4642b4`).

### Added — Security

- **HMAC signed URLs** + audit log tamper-detection chain (`ef42df3`).
- Audit chain hashing + `/api/audit/verify` endpoint (`2168f83`).
- Audit logging wired into gift, mission, costume routes (`b94f741`).

### Added — Reliability & Observability

- `/api/metrics` endpoint + middleware instrumentation (`bc7b78c`).
- Structured logging, slow-query timer, LLM token tracking (`b11b4ab`).
- Redis-backed rate limiting + request-ID tracing (`558b8c2`).
- Auto-compaction helpers + backup verification script (`2058b31`).
- Embedding + affection caches + episode dedup (`811cb82`).

### Added — Infrastructure

- S-tier hardening + amarillo consolidation + VRAM orchestration
  (`9bd58d0`).
- WebSocket multi-device receive, Redis reconnect rebind, fact store
  error handling, Docker resource limits, full subsystem health
  checks, retry-aware DB pool (`d7d0057`).
- Push subscriptions persisted to DB, auth on subscribe, context-aware
  proactive messages (`b0c2e26`).
- Builtin agent tools (memory recall + time) alongside MCP tools
  (`e240eef`).

### Changed

- Rename `companion` → `klukai` across all path references (`35a6c65`).
- Coverage gate stepped up: 35% → 38% → 42% → 45% → 49%.

### Testing

- 537 → 950+ tests across the month. Coverage: 34% → 49%.
- New coverage: `memory_archive`, `personality`, `helpers` detectors,
  `agent_loop`, `llm_json`, `image_gen`, `affection`, `db` pool,
  `fact_extractor`, `events`, `push`, `auth`, `MemoryManager`.

---

## 2026-04 — Initial release

### Added

- Initial companion app with split deploy (amarillo + dominus).
- Character system, affection progression, agentic AI loop, GFL2 UI,
  conversation memory (Redis → Qdrant → PostgreSQL).
- Voice cloning: XTTS v2 with Klukai's VA reference audio on RTX 3090.
- Markdown rendering + image generation via ComfyUI.
- Flutter UI: profile screen, timeline, conversation starters, dorm mode,
  notifications.
- Multi-user, gifts, missions, costumes, Japanese phrases.
- 35 moods across 6 categories with 90-min persistence.
- Klukai LoRA + PhotoMaker + reference images on dominus.
- Animagine XL → NoobAI-XL / Illustrious upgrade for image gen.
- Memory archive feature — Klukai curates her own photo album.

---

## Tier uplift roadmap

This changelog is part of klukai's progression toward S+ tier per
`~/.claude/TIER_RUBRIC.md`. Current floor as of `9fadedf`: **C+**.

- **Phase 1** (active) — Lift to B: mypy + SAST + CHANGELOG + perf baseline.
- **Phase 2** — Lift to A: coverage 49% → 85%, trivy on image, split
  `proactive.py` + `routes.py`, Prometheus scrape, Grafana dashboards,
  SLO definition, top-3 runbooks, restore-tested backups.
- **Phase 3** — Lift to A+: ADRs, OpenTelemetry, dashboards-as-code,
  Renovate, SBOM, cosign signing, chaos drills.
- **Phase 4** — Lift to S/S+: property + mutation testing, SLSA L2/3,
  distroless, CodeQL, circuit breakers, single-cmd DR, spec-driven
  workflow.

---

[Unreleased]: https://github.com/Al-Sarraf-Tech/klukai/compare/main...HEAD
