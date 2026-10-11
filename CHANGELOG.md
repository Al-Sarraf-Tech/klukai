# Changelog

All notable changes to Klukai are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Detailed feature designs live in `docs/superpowers/specs/` and execution plans
in `docs/superpowers/plans/`.

---

## [Unreleased]

### Fixed — standby hardening (2026-10-11)

Dominus (the RTX 3090 host) being powered off is normal. This batch makes that
a state she handles on every surface instead of a cascade of small failures.
Handoff: `docs/handoff/2026-10-11-standby-hardening.md`.

- **companion-core chat path** — a down gateway degrades in-band (sentinel +
  `done`, model `offline`) instead of a traceback and "something glitched"
  per turn; the unanswered user turn is taken back out of the live 16-turn
  window (PostgreSQL keeps it); a sentinel or truncated partial is never
  stored as her turn, replayed as context, or mined (extraction on it used
  to reset her mood to `composed`, credit a streak and store the outage in
  long-term memory). Gateway HTTP errors (503 game block, 400/500) no longer
  mark the gateway down for 15 s. Compaction keeps a turn appended during
  the LLM call. Auth rejection is `accept()` then `close(4001)` so the code
  reaches the client. Each endpoint reads only its own socket.
- **proactive rails** — mission ticks skip instead of pushing "...Static on
  the line" every 15 min all night; the daily recap and mission debrief never
  deliver the sentinel as her words; random lore events stop at 22:45 with a
  quiet-hours guard, dreams honour the goodnight hold, the anniversary remark
  goes through `_can_send()` at 08:00; `_idle_check` requires 90 min of
  silence; affection day boundaries, quiet-day patterns and the recap use the
  Commander's local day, not the container's UTC; `send_push` runs off the
  event loop with a timeout and retires a subscription only on 404/410; an
  Operation Brief slot landing in a goodbye hold is held, not dropped;
  `_deliver` honours a callback that reached nobody.
- **voice** — the sentinel never reaches XTTS (it spent a GPU lease to say it);
  `/api/tts` never forwards the voice host's status (its 401 logged him out)
  and bounds the leased call at 45 s; `/api/stt` returns 502 on upstream
  failure instead of a 200 the PWA swallowed; STT failures send `error`
  frames so clients clear "Listening..."; stage directions `(...)`/`*...*`
  are not read aloud; TTS text clips at a sentence end; `VOICE_URL` with a
  trailing slash no longer 404s STT. Voice service: faster-whisper decoding
  ran on the event loop, freezing `/health` and `/tts` for the clip; STT
  bodies capped at 8 MB; temp files unlinked on every path.
- **memory / images** — a lost Her POV hand-off no longer locks the feature
  (stale rows retired after 20 min; `events.publish` reports subscribers so
  a missed hand-off runs inline; the worker refuses to re-claim a live job;
  bridge timeout 600→900 s to match the real pipeline budget).
  `llm_json.parse_json` handles an object followed by prose and depth-2
  JSON; `extract_facts` tolerates a non-dict parse; `list_memories` fails
  closed; `save_image` removes orphaned files on a failed INSERT; the MCP
  client re-initializes a session that never came up; the image lease budget
  is derived from TTL − acquire deadline − cleanup so finished renders are
  not discarded; Stripe signature checks accept every `v1` during rotation.
- **Flutter PWA** — 401 on history/affection returns to login (no more
  LINK DOWN soft-lock); images render instead of the literal "[IMAGE]";
  a mid-stream error or dropped link freezes the partial bubble and frees
  the composer; `reconnectNow()` reports the link down so history refetches
  after an iOS resume; resume redials only when the page was actually away
  ≥25 s; empty `thinking` clears the spinner; `done.text` replaces the raw
  stream; history timestamps render local; the first token owns the lock;
  `inputLockReason` travels with the lock (no more `// STANDBY...` flicker);
  STT/TTS calls time out and report in her register.
- **companion stage** — her voice survives backgrounding with music off;
  a double blink mid-word no longer stops blinking for the session; the
  sentinel is shown, not spoken; voice-input replies are spoken once; the
  3D renderer pauses behind the hidden portrait; a failed 3D init no longer
  disables the button; `teardown()` no longer hits a TDZ error and leaks a
  WebGL context; portrait refresh survives network errors, races and expired
  signed frame URLs. Push-to-talk: `stop()` racing `start()` no longer leaves
  the mic hot; 60 s safety cap; a recorder error settles the Dart await.
- **dominus gateway** — the token-less tailnet bypass covers only `/v1/*` and
  the catalog listings; model load/unload, the GPU lease and the ComfyUI
  facade require the bearer from every source. `/v1/*` waits at most 120 s
  for the coordination lock, then `503 inference_busy` with `Retry-After`;
  `/health` reports `inference_busy`. Request bodies capped at 64 MiB with
  `mem_limit`/`pids_limit` as the backstop; a non-ASCII bearer is a 401, not
  a 500. The dockerd drop-in's `ExecStartPre` is `-`-prefixed so a missing
  repair binary cannot stop `docker.service`.
- **tgbot** — agent progress now reaches Telegram (stream-json text lives
  under `message.content`); the final result is never throttled away;
  `--verbose` for stream-json; stderr drained concurrently; one agent run at
  a time; ops replies fall back to plain text when Telegram rejects the
  Markdown; edited messages are ignored.
- **tooling** — `scripts/s-tier-audit.sh --diff` implemented; Makefile
  targets `test-tgbot`, `test-ops`, `test-voice`, `test-shell`, `test-all`.

### Changed — file-size hygiene (2026-10-11)

- `docker/core/app/proactive/engine.py` 679→472 LOC; the job table, the
  scheduled-job adapters and the startup catch-up moved to
  `proactive/schedule.py` (`ScheduleMixin`). `events._random_event` and
  `_dream_event` guard chains extracted to `_random_event_blocked` /
  `_dream_blocked` (radon D→C), order preserved. No behaviour change.
- `chat_handlers._handle_message` (radon F 82 → B 6) and
  `background.background_extraction` (F 70 → B) decomposed into named
  phases in their original order; signatures, log lines, WS frames and the
  tests' patch targets unchanged. No behaviour change.

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
