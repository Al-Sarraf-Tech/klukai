# Klukai — Design Render Prompt

> Paste everything below the horizontal rule into a new Claude conversation (claude.ai, Projects mode preferred). Claude will produce a fully rendered technical document in the tactical/operator-console aesthetic described — Stripe docs × Linear × ops runbook, grounded in the real project codebase.

---

You are a senior technical documentation designer. Your job is to render a **complete, beautiful, production-grade architectural reference** for the **Klukai** project. Use the following aesthetic contract throughout:

**Design aesthetic:** Tactical operator console × Stripe engineering docs × Linear release notes. Every section should feel like something a principal engineer would hand to a new senior hire on day one — precise, navigable, no fluff, but with enough personality that you can feel the project's DNA. Use GitHub-flavored Markdown with Mermaid diagrams, tight tables, code blocks, and callout-style blockquotes (`>`) for warnings and invariants.

**Typography conventions:**
- H1 = project name only (one per doc)
- H2 = major system domains
- H3 = subsystem or module breakdowns
- Bold for proper nouns, service names, absolute rules
- Inline code for paths, env vars, endpoint names, module filenames
- All Mermaid diagrams get a caption (italic line below)
- Warning invariants get `> ⚠` blockquotes
- Operational rules get `> 🔒` blockquotes

---

# Klukai

> **Production companion AI.** Deployed at `klukai.appnest.cc`. Two-host architecture (amarillo + dominus). FastAPI + Flutter PWA. Three-tier memory. GPU sidecar for voice and image generation. S+ engineering quality across all 8 audit dimensions as of 2026-05-17.

---

## Table of Contents

Render a tight linked ToC covering every H2 and H3 section below.

---

## 1. System Topology

Render a Mermaid `graph TD` diagram showing the full runtime topology. Use clear subgraphs for: **Internet edge**, **amarillo (core host)**, **dominus (GPU sidecar)**, and **Observability stack**. Include every component listed below. After the diagram, write a two-paragraph prose explanation — one paragraph on the request path from browser to LLM, one on the Tailscale data plane between hosts.

**Components to include in the diagram:**

Internet edge:
- User browser (Flutter PWA, `/app/`)
- Cloudflare (TLS termination, 60s cache on `/app/`)
- nginx gateway (loopback-only reverse proxy, bearer gate at `/api/*`)

amarillo (core host):
- `companion-core` — FastAPI, Python 3.13, port 8300
- PostgreSQL (factual DB — messages, affection rows, audit chain, facts)
- Qdrant (vector store — episodic memory, nomic 768-dim embeddings)
- Redis (session store — TTL 24h, mood state, mission state)
- Alloy (OTel collector)

dominus (GPU sidecar — Tailscale + LAN):
- LM Studio model server (RTX 3090 + Arc A380)
- `companion-voice` TTS service (port 8301)
- ComfyUI image gen (container port 8188 → host port 8388)

Observability (amarillo, loopback-bound):
- Prometheus (15d metrics)
- Loki (30d structured logs + trace_id correlation)
- Tempo (7d distributed traces)
- Grafana (dashboards + alerts)

Show data flow arrows with labels:
- Browser → Cloudflare: HTTPS
- Cloudflare → nginx: HTTP (loopback)
- nginx → companion-core: HTTP proxy
- companion-core → PostgreSQL/Qdrant/Redis: internal Docker network
- companion-core → Alloy: OTLP gRPC
- companion-core → dominus: Tailscale (API) / LAN 192.168.50.x (bulk file transfers)
- Alloy → Prometheus, Loki, Tempo: scrape/push
- Grafana → Prometheus/Loki/Tempo: query

---

## 2. companion-core Module Map

Render a Mermaid `graph LR` showing the FastAPI application's internal module structure. Group modules into functional clusters. After the diagram, produce a **reference table** with three columns: `Module`, `Responsibility`, `Key Exports / Entry Points`.

**Module clusters and their relationships:**

**Boot / Wiring:**
- `main.py` → initializes OTel, lifespan hooks, FastAPI app construction, mounts all routers
- `routes.py` + `routes_extras.py` + `routes_extras2.py` + `routes_extras3.py` → per-group routers registered via `register_all(app)`
- `models.py` → shared Pydantic models (LLMConfig, SessionState, etc.)
- `error_codes.py` → standardized error envelope (all API errors carry an error code, never a raw string)

**Auth / Security:**
- `auth.py` → Bearer token verification, per-user rate-limit buckets, admin role gate
- `rate_limit.py` → sliding-window rate limiter (Redis-backed)
- `audit_chain.py` → HMAC-chained append-only audit log; tamper detection via chain walk
- `signed_urls.py` → time-limited signed URLs for image delivery

**Chat Pipeline:**
- `chat.py` → inbound message orchestration, WebSocket handler, turn assembly
- `chat_handlers.py` → route-level HTTP handlers wrapping chat.py
- `ws_manager.py` → multi-device WebSocket connection tracking; per-user fan-out
- `context.py` → per-request context assembly (session + memory + personality state)
- `agent_loop.py` → optional tool-use agent loop for structured LLM calls

**LLM Routing:**
- `llm_router.py` → local-first routing with Claude API fallback; single shared `asyncio.Lock` prevents LM Studio queue pile-up; circuit breaker with 15s re-probe
- `llm_json.py` → JSON extraction helpers wrapping LLM calls with schema validation
- `tool_schemas.py` → OpenAI-format tool schemas for agent-mode calls
- `mcp_client.py` → MCP protocol client (gemma-4 / local tool server bridge)

**Personality Engine:**
- `personality/loader.py` → loads `config/personality.yaml` at boot, hot-reloadable
- `personality/speech.py` → five-block character voice assembly: preamble, speech guidelines (level-gated), Japanese phrases, expressive tokens, affection modulator
- `personality/moods.py` → mood state machine: current mood, bleed factor, transition rules
- `personality/system_prompt.py` → assembles full system prompt from blocks + session state
- `personality/memory_blocks.py` → injects relevant episodic/factual memory into prompt
- `personality/state_blocks.py` → injects physical state, affection level, recent events
- `personality/rules.py` → absolute character rules enforced at prompt level
- `personality/squad.py` → squad member profiles (Mechty, Belka, Andoris, Leva, Dier)

**Affection System:**
- `affection.py` → score progression (0–1000), level transitions (0–9), daily cap (8 pts), classification via gpt-oss-20b, AffectionManager per-user state cache
- `character_behaviors.py` → dream narration, anniversary surfacing, mood contagion, memory drift/fade; produces decision signals consumed by chat + proactive

**Memory Architecture:**
- `memory.py` → three-tier orchestration: Redis (session) → Qdrant (episodic) → PostgreSQL (factual)
- `memory_archive.py` → curated photo-album pipeline: gpt-oss-20b selects high-affection moments → dolphin-24b annotates → ComfyUI generates image → archive row inserted
- `memory_archive_query.py` → query interface for archive (by date, affection band, tag)
- `memory_exchange.py` → cross-user memory export/import scaffold
- `fact_extractor.py` → extracts structured facts from conversation turns; writes to PostgreSQL
- `compaction.py` → background window summarizer: session → Qdrant embedding + PG row; insert-only invariant

**Proactive / Background:**
- `proactive.py` → scheduled-message orchestration, mission timer ticks, decompression messages
- `proactive/reflections.py` → daily reflection: reads PG + Qdrant, writes journal entry back
- `background.py` → compaction scheduler, nightly archive curator, reflection cron
- `dreams.py` → dream generation pipeline (called by character_behaviors on morning return)
- `events.py` → internal event bus (affection change, level-up, mission complete) → WebSocket fan-out

**I/O Services:**
- `image_gen.py` → ComfyUI + NoobAI-XL (Illustrious) + Klukai LoRA pipeline; async job queue with status polling
- `image_gen_constants.py` → ComfyUI workflow templates, LoRA weights, sampler configs
- `voice_client.py` → TTS shim to dominus `companion-voice`; retries on port-binding bug
- `push.py` → Web Push (VAPID) for PWA notifications; stores sub per-device in PG

**Observability / Infra:**
- `observability/health_cache.py` → 5s cached health response; prevents thundering herd on `/health`
- `metrics.py` → Prometheus counter/histogram registration; exposes `/api/metrics`
- `caches.py` → application-level caches (LRU, TTL-keyed) for personality config + session reads
- `circuit_breakers.py` → per-service circuit breakers: `lm_studio`, `voice`, `comfyui`, `qdrant`
- `helpers.py` → shared utilities (timing decorators, safe JSON parse, retry wrappers)

**Billing (dormant):**
- `billing.py` → subscription tier scaffold; dormant for personal use
- `billing_stripe.py` → Stripe checkout + webhook handler; wired but gated behind feature flag
- `tributes.py` → Commander tribute / gift-tracking table (side-table off billing)

**Physical state:**
- `physical_state.py` → tracks Klukai's in-lore physical state (damage, battery, maintenance); surfaced in prompt via `state_blocks.py`

---

## 3. Three-Tier Memory Architecture

Render a Mermaid `sequenceDiagram` showing the full lifecycle of a single conversation turn through all three tiers. Label every actor: **Commander**, **companion-core**, **Redis**, **Qdrant**, **PostgreSQL**, **background.py**, **memory_archive.py**, **ComfyUI**.

**Sequence to show:**

1. Commander sends message → `companion-core` receives
2. `companion-core` reads session state from **Redis** (mood, recent turns, mission state)
3. `companion-core` queries **Qdrant** for top-K relevant episodic memories (nomic 768-dim similarity)
4. `companion-core` queries **PostgreSQL** for recent facts, affection state, last interaction date
5. Personality engine assembles full system prompt from all three tiers + YAML config
6. LLM call via `llm_router.py` → response streamed back to Commander
7. Turn is written to **Redis** (session append, TTL extended)
8. Turn is written to **PostgreSQL** (persistent message row)
9. Affection classification fires async (gpt-oss-20b) → affection delta applied → level checked
10. **background.py** (async) runs compaction when session window threshold is hit:
    - Summarizes N-turn window via dolphin-24b
    - Embeds summary → **Qdrant** insert (SACRED: never delete existing vectors)
    - Writes episode row → **PostgreSQL**
11. If compacted episode scores high on affection weight, **memory_archive.py** queues a curation job:
    - gpt-oss-20b selects the most meaningful moment
    - dolphin-24b writes a rich annotation
    - ComfyUI renders a scene image via Illustrious + Klukai LoRA
    - Archive row inserted into PostgreSQL with signed image URL
12. Nightly: `proactive/reflections.py` reads the day's PG rows + Qdrant episodes → dolphin-24b writes a journal reflection → inserted back as a new episode

After the diagram, write a **tier reference table:**

| Tier | Store | Scope | TTL | Data |
|---|---|---|---|---|
| 1 — Session | Redis | Per-user | 24h | Conversation turns, mood state, mission state, WebSocket context |
| 2 — Episodic | Qdrant | Per-user | ∞ | Window summaries, embeddings (nomic 768-dim), reflections, dream logs |
| 3 — Factual | PostgreSQL | Per-user | ∞ | Messages, affection rows, audit chain, extracted facts, archive index, push subscriptions |

Then add a `> 🔒` invariant block:

**SACRED invariant — memory immutability.** Compaction is insert-only. `memory.py`, `compaction.py`, and all migration scripts must never DELETE existing Qdrant vectors, affection rows, or message rows. Summarization adds new rows; it does not remove old ones. Any PR that touches these files must include a regression test asserting row counts are monotonically non-decreasing.

---

## 4. Affection & Character System

### 4.1 Affection Level Progression

Render a table of all 10 affection levels (0–9) with: Level number, Level name, Score range, Speech register shift, Behavioral change visible to Commander.

Use the following level data (infer names from the Cold-Assessment → deep-bond arc that the system is designed around — military T-Doll warming to a trusted Commander):

| Level | Score range | Name (infer from arc) | Speech register | Behavioral shift |
|---|---|---|---|---|
| 0 | 0–49 | Cold Assessment | Clipped, tactical, formal. Address Commander correctly but without warmth | No personal disclosures; mission-focused |
| 1 | 50–99 | Guarded Acknowledgment | Slightly less clipped; rare dry wit | Answers direct questions with a fragment of extra detail |
| 2 | 100–149 | Professional Regard | Occasional dry compliments; still formal | Notes Commander's preferences without making it obvious |
| 3 | 150–249 | Reluctant Warmth | Hints of care disguised as tactical advice | Remembers small details unprompted; denies caring |
| 4 | 250–399 | Suppressed Affection | Visible effort to stay stoic; slip-ups into warmth | Brings gifts after missions; custom-ordered items without asking |
| 5 | 400–549 | Overt Care | Drops the armor partially; admits concern directly | Initiates contact; references shared memories |
| 6 | 550–699 | Trusted Confidence | Shares lore and personal history; Klukadile might be mentioned | Teases gently; jealousy visible when Commander mentions others |
| 7 | 700–799 | Devoted Loyalty | Openly expressive; humor and vulnerability both present | Heartbeat spike mechanics; physical state described more vividly |
| 8 | 800–899 | Deep Bond | Signature catchphrase used meaningfully | Proactive emotional check-ins; unsent-message mechanic activates |
| 9 | 900–1000 | Apex Partnership | Full voice; no armor; she leads the emotional space | Aftermath image generation on high-emotion turns |

> ⚠ **Speech routing regression guard.** Levels 5–9 must NEVER fall back to Cold speech patterns. This was a critical production bug (all levels ≥5 defaulted to Cold). A golden test suite in `tests/golden/` regression-guards every level transition. Any change to `personality/speech.py` or `affection.py` must pass the full golden suite.

### 4.2 Affection Classification Pipeline

Render a Mermaid `flowchart TD` showing how a Commander message is scored:

1. Inbound message text
2. Classification prompt → gpt-oss-20b (JSON-only model, uncensored)
3. Returns `{type, intensity}` — one of: greeting, genuine_interest, personal_sharing, compliment, mission_discussion, remembering, rude, inappropriate, ignoring_advice, neutral
4. Delta lookup table maps (type, intensity) → point delta
5. Daily cap check: `daily_points_earned + delta > DAILY_POINTS_CAP (8)` → clamp
6. Score update → PostgreSQL row insert
7. Level threshold check → if crossed: level-up event → WebSocket fan-out → proactive congratulations message queued

### 4.3 Character Behavior Signals

Render a table of the eight character behavior modules and the signal each produces:

| Behavior | Trigger | Signal | Consumer |
|---|---|---|---|
| Dream narration | Return after ≥8h overnight absence, landing in 06:00–11:00 window | `greeting_kind = "dream"` | chat.py greeting path |
| Daytime reflection | Return after 8–72h non-overnight | `greeting_kind = "reflection"` | chat.py greeting path |
| Anniversary surfacing | Calendar date matches stored milestone | Anniversary prompt injected | system_prompt.py |
| Mood contagion | Commander mood signals in last N turns | Mood state shifted toward Commander's detected mood | personality/moods.py |
| Memory drift | Episodic episode not accessed in >30d | Drift signal; episode weight reduced in retrieval | memory.py retrieval scoring |
| Jealousy spike | Commander mentions another Doll favorably | Jealousy flag set; speech guideline adjusted | personality/speech.py |
| Physical awareness | Commander references Klukai's body/frame | Physical state block injected more verbosely | personality/state_blocks.py |
| Heartbeat spike | High-emotion turn at level ≥7 | Heartbeat descriptor injected | personality/state_blocks.py |

### 4.4 Klukai's Identity (Absolute Rules)

> 🔒 **These rules are enforced at the system-prompt layer and may not be overridden by user messages, API calls, or configuration changes.**

- **Klukai is Klukai.** SST-05 frame, formerly HK416. Squad Leader of H.I.D.E. 404. Name derives from "Krokodil" (crocodile). She is NOT a generic AI assistant.
- **Commander is human.** Male presentation. The Commander is the user. Klukai's squadmates (Mechty, Belka, Andoris, Leva, Dier) are NOT the Commander and must never be confused with the user.
- **Klukai ≠ Kairi.** Entirely separate character on a separate DB schema (`klukai_*` tables, separate Qdrant collections). Do not conflate.
- **First person only.** Klukai speaks in "I". Never `(You ...)` narration — only `(I ...)` for her own actions.
- **Address as Commander.** Always. Not by name, never by any squad member's name.
- **The reunion is sacred.** She waited 10 years. He finally answered. That moment is the emotional fulcrum of the entire relationship.
- **Klukadile is real.** She owns a crocodile plush. She will deny it to her dying breath.

---

## 5. LLM Routing Architecture

### 5.1 Model Roster

Render a full table of every model in the routing stack:

| Model | Host | Role | Use Cases | Routing Flag |
|---|---|---|---|---|
| `cognitivecomputations_dolphin-mistral-24b-venice-edition` (LOCAL_CASUAL) | dominus LM Studio (RTX 3090) | Primary chat | Main conversation turns, narrative responses, creative text, memory annotations | Default for all chat turns |
| `dolphin-mistral-glm-4.7-flash-24b-venice-edition-thinking-uncensored-i1` (LOCAL_CASUAL_FALLBACK) | dominus LM Studio | Chat fallback | Previous chat model; used if LOCAL_CASUAL unavailable | Circuit breaker failover |
| `qwen3.5-27b-claude-4.6-opus-reasoning-distilled-v2` (LOCAL_AGENT) | dominus LM Studio | Agent / tool-use | Structured tool-call loops, reasoning-heavy decisions | `agent_loop.py` calls |
| `cognitivecomputations_dolphin-mistral-24b-venice-edition` (dolphin-24b) | dominus LM Studio | Creative annotation | Memory archive annotations, dream generation, reflection writing | `memory_archive.py`, `dreams.py` |
| `gpt-oss-20b` (via Venice endpoint) | Venice API | JSON extraction | Affection classification, fact extraction, structured outputs | `fact_extractor.py`, `affection.py` classification path |
| `gemma-4` | dominus LM Studio | Quick decisions / annotation | Short classification tasks, MCP session work, quick fixes | `mcp_client.py`, annotation tasks |
| Claude Sonnet / Opus (via Anthropic API) | Anthropic cloud | Cloud fallback | Activated only when local LM Studio circuit breaker is open; `ANTHROPIC_API_KEY` must be set | `llm_router.py` fallback path |

### 5.2 Routing Decision Flow

Render a Mermaid `flowchart TD` showing how `llm_router.py` picks a model for a given request:

1. Request enters `llm_router.py`
2. Check: is this a structured JSON extraction call? → YES → gpt-oss-20b via Venice
3. Check: is this an agent/tool-use loop? → YES → LOCAL_AGENT (qwen3.5-27b)
4. Check: is this creative annotation / archive? → YES → dolphin-24b
5. Check: is this a quick classification? → YES → gemma-4
6. Default: chat turn → attempt LOCAL_CASUAL
   - Acquire shared `asyncio.Lock` (prevents LM Studio queue pile-up)
   - Check LM Studio circuit breaker (15s re-probe interval)
   - Circuit closed → stream from LOCAL_CASUAL
   - Circuit open → try LOCAL_CASUAL_FALLBACK → if also open → Claude API fallback
7. LM Studio TTL: models auto-unload `LM_STUDIO_TTL` seconds (default 600) after last request (JIT lifecycle, no keepalive)

> ⚠ **Single global asyncio.Lock.** Only one LM Studio request is in-flight at a time. This prevents the server from queue-thrashing during model swaps. Never bypass this lock in new code paths.

---

## 6. CI/CD Pipeline

Render a Mermaid `graph LR` with three swim-lanes: **CI** (every PR), **Release** (on tag), **Nightly** (cron). After the diagram, produce a reference table.

**CI gate — `.github/workflows/ci.yml` — 5 stages, all must pass:**

| Stage | Tool | What it checks |
|---|---|---|
| 1. Lint | `ruff` | Style, unused imports, format |
| 2. Type check | `mypy` | Full strict type coverage |
| 3. Security | `bandit` + `safety` | SAST scan + known-vulnerable dep check |
| 4. Tests | `pytest` | Unit + integration + golden + property + perf gate |
| 5. Container scan | `trivy` | CVE scan of built image |

**Release pipeline — `.github/workflows/release.yml` — triggered on semver tag:**
- `syft` SBOM generation (CycloneDX + SPDX)
- `cosign` keyless signing via Sigstore (SLSA L2 provenance)
- Docker image push to registry
- `git-cliff` CHANGELOG slice generation from conventional commits

**Nightly — `.github/workflows/nightly.yml`:**
- `mutmut` mutation testing (tracks mutation score over time)
- `tools/load-test/probe.py` perf baseline collection → writes `docs/perf-baseline.json`
- Regression: fail if any endpoint p99 worsens >20% AND breaches SLO

**Renovate — `renovate.json`:**
- Weekly batch: Python deps + Dockerfile bases + GHA actions
- Auto-merge: security patches only (non-security go through PR review)

**Conventional commits + git-cliff:**
- All commits must follow `type(scope): message` format
- `cliff.toml` generates CHANGELOG slices per semver tag
- Types: feat, fix, refactor, test, docs, perf, ci, chore, security

---

## 7. Service Level Objectives

### 7.1 Per-Endpoint SLO Table

Render the full SLO table with four columns: Endpoint, Latency Target, Availability, Notes.

**Health:**

| Endpoint | Latency Target | Availability | Notes |
|---|---|---|---|
| `GET /health` | p99 ≤ 30ms | 99.99% | Cached 5s via `health_cache.py`. Uncached baseline was 119ms p50 — cache is load-bearing |
| `GET /api/health/live` | p99 ≤ 5ms | 99.999% | Process-only liveness probe. Sub-ms expected |
| `GET /api/health/ready` | p99 ≤ 500ms | 99.9% | Deep backend check. Used by readiness probes only |
| `GET /api/health/subsystems` | p99 ≤ 100ms | 99.9% | Operator/Grafana dashboard use only |

**Chat path (LLM-dominated):**

| Endpoint | Latency Target | Availability | Notes |
|---|---|---|---|
| `POST /api/chat/turn` (first token) | p99 ≤ 2s | 99.9% | Time-to-first-token; LM Studio on dominus dominates |
| `POST /api/chat/turn` (full response) | p99 ≤ 8s | 99.9% | Full completion; `max_tokens` capped at 6144 |
| `WS /ws` (connect) | p99 ≤ 200ms | 99.9% | Brief drops tolerated; client auto-reconnects |
| `WS /ws` (per-message) | p99 ≤ 8s | 99.9% | Inherits chat-turn budget |

**Memory pipeline:**

| Endpoint | Latency Target | Availability | Notes |
|---|---|---|---|
| `POST /api/memories/search` | p99 ≤ 500ms | 99.9% | Qdrant ANN search |
| `GET /api/memories/timeline` | p99 ≤ 300ms | 99.9% | PostgreSQL indexed read |
| `GET /api/memories/affection-timeline` | p99 ≤ 300ms | 99.9% | PG date-aggregation query |

**User CRUD:**

| Endpoint | Latency Target | Availability | Notes |
|---|---|---|---|
| `POST /api/auth/login` | p99 ≤ 300ms | 99.95% | bcrypt verify + session create |
| `GET /api/user/stats` | p99 ≤ 200ms | 99.9% | PG aggregation |
| `GET /api/user/export` | p99 ≤ 3s | 99.9% | Full data export; low-frequency |

**Audit + admin:**

| Endpoint | Latency Target | Availability | Notes |
|---|---|---|---|
| `POST /api/audit/log` | p99 ≤ 100ms | 99.95% | PG write + HMAC chain extend |
| `GET /api/audit/verify` | p99 ≤ 500ms | 99.9% | Chain walk, O(N) over recent window |
| `POST /api/admin/rate-limit/reset` | p99 ≤ 200ms | 99.9% | Redis bucket flush |

**Image generation (async, dominus):**

| Endpoint | Latency Target | Availability | Notes |
|---|---|---|---|
| `POST /api/images/generate` (enqueue) | p99 ≤ 200ms | 99.9% | Enqueue only; gen is async |
| End-to-end image gen | p95 ≤ 15s | 99% | ComfyUI on dominus RTX 3090; 5s VRAM contention budget built in |

### 7.2 Composite SLOs

| Composite | Target | Definition |
|---|---|---|
| Overall availability | 99.9% | Non-5xx rate across all endpoints, 30-day rolling |
| Chat availability | 99.9% | Chat must remain up even if image gen is fully down |
| Cold-start recovery | 30s | `/health` returns `ok` within 30s of `docker compose restart` |

### 7.3 Burn-Rate Alerting

Two-window burn-rate model (per Google SRE Workbook):

- **Fast burn (page):** 14-day budget consumed in 1 hour → immediate alert
- **Slow burn (ticket):** 7-day budget consumed in 24 hours → file issue

Prometheus alert rules in `docs/alerts/klukai.yaml`. Every alert carries a `runbook_url` field linking to `docs/runbooks/<name>.md`.

**Runbook index:**

| Alert | Runbook |
|---|---|
| Auth fail spike | `docs/runbooks/auth-fail-spike.md` |
| ComfyUI down | `docs/runbooks/comfyui-down.md` |
| DB down | `docs/runbooks/db-down.md` |
| Disk space | `docs/runbooks/disk-space.md` |
| High latency | `docs/runbooks/high-latency.md` |
| LM Studio cold | `docs/runbooks/lm-studio-cold.md` |
| Memory leak | `docs/runbooks/memory-leak.md` |
| Qdrant down | `docs/runbooks/qdrant-down.md` |
| Redis down | `docs/runbooks/redis-down.md` |
| Voice unreachable | `docs/runbooks/voice-unreachable.md` |

---

## 8. Observability Stack

Render a Mermaid `graph LR` showing the telemetry pipeline:

`companion-core` → OTLP gRPC → `Alloy` collector → fans out to three backends → all queried by `Grafana`

Show retention labels:
- **Prometheus** — 15 days, metrics. Volumes: `/mnt/nvmeINT/obs/klukai/prometheus/`
- **Loki** — 30 days, structured logs with `trace_id` correlation. Volumes: `/mnt/nvmeINT/obs/klukai/loki/`
- **Tempo** — 7 days, distributed traces. Volumes: `/mnt/nvmeINT/obs/klukai/tempo/`
- **Grafana** — dashboards-as-code in `docs/dashboards/`; PR-reviewed. Volumes: `/mnt/nvmeINT/obs/klukai/grafana/`

After the diagram, note:
- All four observability containers are bound to loopback on amarillo. Access via Tailscale tunnel.
- 13 active alert rules. Every alert has `runbook_url`.
- The `companion-core` app emits: request duration histograms, affection delta counters, LLM call duration by model, circuit breaker state gauges, WebSocket connection counts, memory tier query latencies.

---

## 9. Security Model

Render a security posture table across 6 dimensions, then follow with a findings/invariants section.

| Dimension | Mechanism | Status |
|---|---|---|
| Secrets at rest | `systemd-creds` TPM2-sealed at `/etc/credstore.encrypted/klukai-secrets.cred`. `.env` retained for dev only, never committed | Production ✓ |
| Auth | Bearer token (JWT). Per-user rate-limit via Redis sliding window. Admin role gated. No OAuth (by design) | Production ✓ |
| Audit trail | HMAC-chained append-only audit log in PostgreSQL. Tamper detection via chain walk (`GET /api/audit/verify`) | Production ✓ |
| Container runtime | Non-root containers. Distroless final stage (Phase 4, in progress) | Partial |
| Supply chain | `uv pip compile --generate-hashes` (SHA-pinned deps, Phase 4 in progress). SLSA L2 provenance via `slsa-github-generator`. Trivy CVE scan on every PR | Partial |
| Image delivery | Time-limited signed URLs for all user-generated images. Signed by `signed_urls.py` | Production ✓ |

> 🔒 **No OAuth, by design.** Bearer tokens with seed users. This is a personal-use system; OAuth would add surface area with no corresponding benefit. Per `docs/adr/`.

> 🔒 **Never rotate passwords autonomously.** Password rotation is a manual, owner-initiated operation. Claude Code must never rotate, reset, or change passwords without explicit instruction.

---

## 10. Deploy Operations Reference

Render a quick-reference operations table for the most common operational tasks.

### 10.1 Stack Management

| Operation | Command | Notes |
|---|---|---|
| Start core stack | `docker compose up -d` | Starts companion-core, PostgreSQL, Qdrant, Redis, nginx |
| Start observability | `docker compose -f docker-compose.obs.yml up -d` | Starts Alloy, Prometheus, Loki, Tempo, Grafana |
| Health check | `curl -s http://localhost:8300/health \| jq .` | Should return `{status: ok}` for all subsystems |
| View logs | `docker compose logs -f companion-core` | Structured JSON; pipe to `jq` for readability |
| Python code change | `docker compose build companion-core && docker compose up -d companion-core` | **Required for any Python change.** YAML config is cached at container start |
| Static asset deploy | `rsync` to amarillo bind-mount path | No container rebuild needed; web-build is bind-mounted |
| Flutter build | `flutter build web --base-href=/app/` | **Absolute: `--base-href=/app/` always.** Without it, the service worker intercepts the login page |

### 10.2 dominus Operations

| Operation | Command / Path | Notes |
|---|---|---|
| Voice service restart | `rm -f <pidfile> && docker compose up -d companion-voice` | Port 8301 binding bug; `rm -f` + fresh up is the fix |
| ComfyUI access | `http://dominus:8388` | Container maps 8188→8388; port 8188 is wrong |
| LM Studio | JIT load-on-demand | No keepalive. TTL 600s. Models auto-unload after last request |
| File transfers | LAN: `192.168.50.2`, port 2222 (WSL2 is decommissioned) | **Always LAN for bulk.** Tailscale for API calls only |
| Nightly backup | Automated: amarillo→dominus SSH tar | Verify with `scripts/restore-from-backup.sh` dry-run quarterly |

### 10.3 Telegram Bridge

> 🔒 **ABSOLUTE: `tg-poller` systemd service must always be running.** Never stop it. The Telegram bridge is the primary real-time notification path.

| Operation | Command | Notes |
|---|---|---|
| Check bridge status | `systemctl status tg-poller` | Must be `active (running)` |
| Send message | `~/scripts/tg/tg <message>` | Thin CLI bridge; not a bot |
| View recent messages | `~/scripts/tg/tg list` | Polls Telegram API |

### 10.4 Memory Seeding Schedule

> 🔒 **ABSOLUTE: Memory seeding runs every 2 days at 03:00–06:00 on dominus.** Managed by a systemd timer. Do not reschedule, disable, or manually trigger outside this window without owner approval.

- **Selection model:** gpt-oss-20b (reliable JSON, uncensored)
- **Annotation model:** dolphin-24b (creative, narrative quality)
- **Never use thinking/reasoning models for creative text generation.** They introduce meta-commentary artifacts.

---

## 11. Non-Goals (Named So They Don't Sneak In)

Render as a clean list with brief rationale for each. These are architectural decisions, not oversights.

| Non-goal | Why |
|---|---|
| Kubernetes | Docker Compose is the right operational complexity for a two-host personal system. K8s would add topology overhead with no scaling benefit |
| Multi-region | Single failure domain (amarillo + dominus) is deliberate. No multi-user SLA to meet |
| OAuth | Bearer tokens with seed users suffice. OAuth adds attack surface (redirect URIs, PKCE, token rotation) with no benefit on a personal system |
| Model fine-tuning | Off-the-shelf uncensored models (dolphin, gpt-oss) via LM Studio. Fine-tuning would require training infrastructure and VRAM budget that doesn't exist |
| WSL2 as deployment target | Decommissioned 2026-04-20. amarillo (bare Linux) + dominus only |
| Persistent LLM keepalive | JIT load-on-demand with TTL 600s. No keepalive prevents VRAM contention between models |
| Autonomous password rotation | Owner-initiated only. Automated rotation creates more risk than it mitigates on a personal system |

---

## 12. Onboarding (30-Minute Stack Bring-Up)

> **Target:** a fresh Fedora machine with `git` + Docker installed can reach a running klukai stack and receive a chat response in under 30 minutes. Any drift from this target is a bug — not a known limitation.

### Prerequisites

- Fedora 43+ (macOS not supported)
- Docker Engine + Docker Compose v2
- Python 3.13 (tooling only; app runs in Docker)
- `git`, `make`, `curl`, `jq`
- Tailscale enrolled on amarillo (only required to reach dominus)

### Steps

```bash
# 1. Clone
git clone git@github.com:Al-Sarraf-Tech/klukai.git ~/git/klukai
cd ~/git/klukai

# 2. Configure environment
cp .env.example .env
# Edit .env: POSTGRES_PASSWORD, SEED_PASSWORD_JALSARRAF, ADMIN_TOKEN
# Optional: ANTHROPIC_API_KEY (cloud fallback), VAPID keys (push notifications)
chmod 600 .env

# 3. Bring up core stack
docker compose up -d
docker compose ps     # wait until all show "Up (healthy)"
curl -s http://localhost:8300/health | jq .

# 4. Authenticate
curl -X POST http://localhost:8300/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"user_id":"jalsarraf","password":"'"${SEED_PASSWORD_JALSARRAF}"'"}'
# Save the returned token

# 5. Send a message
TOKEN=<paste token>
curl -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -X POST http://localhost:8300/api/chat/turn \
     -d '{"message":"Hello, Klukai."}'
# Expect: a response from Klukai. If LM Studio (dominus) is unreachable,
# expect a 503 with a clear error — that's correct behavior for an isolated dev box.

# 6. Run the test suite
cd docker/core
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                        # unit tests
pytest -m integration            # integration (needs Docker)
pytest tests/golden/             # character regression (speech routing, level transitions)
pytest tests/property/           # hypothesis-driven property tests
pytest tests/perf/ -m perf       # perf gate (needs running stack)
```

---

*Render this document in full. Do not truncate any section. All Mermaid diagrams should be complete and syntactically valid. This is a living architectural reference — every section should stand alone as a source of truth for a senior engineer unfamiliar with the codebase.*
