# Changelog

All notable changes to Klukai are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Detailed feature designs live in `docs/superpowers/specs/` and execution plans
in `docs/superpowers/plans/`.

---

## [Unreleased]

### Added

- **CI: type-check + security gates** (`9fadedf`, 2026-05-16) — Phase 1 of S+
  tier uplift. Adds `mypy`, `bandit`, `safety` jobs to `.github/workflows/ci.yml`.
  Per-module mypy overrides in `docker/core/mypy.ini` track Phase 2 type-debt
  TODOs (`metrics`, `physical_state`, `memory_archive`, `llm_router`).
- **Loopback bind + autoheal** (`8679409`, 2026-05-16) — `companion-core`
  binds `8300` to `127.0.0.1` only; nginx gateway is the public ingress.
  `autoheal: "true"` labels on core + gateway (autoheal daemon opt-in).

### Changed

- `chat.py`: explicit type annotations on `episode_memories`, `rel_facts`,
  `recalled_exchanges` short-message paths (mypy baseline).
- `requirements.txt`: pin `types-PyYAML>=6.0` for mypy `yaml` stubs.

### Security

- Bandit `# nosec B608` annotations on parameterized f-string SQL in
  `audit.py` + `memory_archive.py`, with justification comments. All
  user values still bound via `%s`; f-string interpolates only
  allow-listed column / predicate names.

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
