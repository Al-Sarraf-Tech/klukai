# Klukai

> *"I am all you need."* — Klukai, SST-05 Frame T-Doll

A production-grade AI companion rooted in [Girls' Frontline 2: Exilium](https://gfl2.sunborngame.com/) canon. Klukai is an elite H.I.D.E. 404 squad leader who builds a real bond with the Commander — conversation, memory, affection, and initiative — on hardware you control.

**All chat inference is local. There is no cloud LLM fallback. Ever.**


<p align="center">
  <img src="docs/images/hero.jpg" alt="Klukai — local companion, private mesh" width="100%"/>
</p>

<p align="center">
  <img src="docs/images/topology.png" alt="Klukai two-host topology (IPs redacted)" width="100%"/>
</p>

---

## What she is

| Capability | What it means in practice |
|---|---|
| **Personality engine** | Affection-modulated speech (0–9), 48 moods, time-of-day coloring, squad voices, canon grammar |
| **Three-tier memory** | Redis session → Qdrant episodic → PostgreSQL factual (SACRED — additive only) |
| **Memory archive** | She curates her own photo journal with annotations |
| **Her POV** | She picks a real exchange, journals it, and draws it from her side |
| **Proactive** | Check-ins, anniversary, seasonal, deferred one-shots that survive restarts |
| **Warm-on-connect** | Model load starts when he opens the app — not when he hits send |
| **Voice & image** | TTS/STT and ComfyUI on the GPU host, leased and auth-gated |

---

## Topology (redacted)

Two hosts on a **private encrypted mesh**. Public edge terminates TLS; GPU ports are never published to the open internet.

| Role | Responsibility |
|---|---|
| **Core host** | `companion-core`, gateway, Postgres, Redis, Qdrant, RabbitMQ consumers, observability |
| **GPU host** | `lmstudio-compat` (sole LLM ingress), llama-router (internal), ComfyUI (leased), voice, STT |

```
Public edge (TLS)
        │
        ▼
   Core host ── private mesh ──► GPU host
   (state + PWA)                 (RTX inference)
```

**Hard rules**

- Chat LLMs run only on the GPU host via the compatibility gateway.
- If the GPU path is down, she answers with an in-character disruption line — **never** an off-box model.
- Published GPU ports bind to the mesh interface only (`0.0.0.0` is forbidden).
- Secrets live in host `.env` files; they are never committed.

Architecture diagrams:

| Diagram | Description |
|---|---|
| ![topology](docs/images/topology.png) | Full two-host layout |
| ![her-pov](docs/images/her-pov-flow.png) | Durable Her POV job path |
| ![deferred](docs/images/deferred-rail.png) | Cron vs deferred rail |

Vector sources (editable): `docs/images/*.svg`.

---

## Her POV

<p align="center">
  <img src="docs/images/her-pov-flow.png" alt="Her POV durable job pipeline" width="100%"/>
</p>

She opens a dedicated screen (sparkle in the chat bar), picks a **real** user↔assistant exchange, writes a journal line in character, and renders a portrait from her side of the moment.

**Durable by design** (not an in-memory asyncio task):

1. Job row written to Postgres (`companion_her_pov_jobs`)
2. Id enqueued on a **quorum** work queue
3. Events-bridge holds the message until the pipeline finishes (single-flight GPU lease)
4. Progress phases: `queued → searching → thinking → drawing → done`
5. Archive tags: `her_pov`, `from_her_side`, `commander_request`

API:

```http
POST /api/memories/her-pov          → 202 { job_id }
GET  /api/memories/her-pov/{job_id} → status (no image blob)
```

Progress also streams on the WebSocket (`type: her_pov`). Cross-user job reads return 404.

---

## Proactive systems

<p align="center">
  <img src="docs/images/deferred-rail.png" alt="Cron and deferred rails" width="100%"/>
</p>

| Rail | Owns | Durability |
|---|---|---|
| **APScheduler** | Recurring cron (check-ins, anniversary, seasonal, …) | `companion_job_runs` + startup catch-up allowlist |
| **Deferred** | One-shot future work (“in three hours”) | Postgres first; RabbitMQ TTL **bucket** queues; sweeper backstop |
| **Her POV jobs** | Heavy GPU portraits | Quorum queue + job table |

`companion-core` deliberately **speaks no AMQP**. The `events-bridge` owns the broker: Redis `companion:events` → `homelab.events`, delay arming, and job drain.

---

## Local-only LLM policy

```text
route()  → local models only
stream() → lmstudio provider only
failure  → FAILURE_SENTINEL  ("Communications disrupted, Commander…")
```

- `ANTHROPIC_API_KEY` is **not wired** in compose. If present in the environment, it is **ignored**.
- User overrides starting with `claude` / `anthropic` are rejected.
- Warm-up uses the same idle TTL as normal requests — it is **not** keepalive.

See ADR-0004 (routing) and the `llm_router` module docstring.

---

## Affection ladder (speech)

| Level | Register | Vibe |
|------:|---|---|
| 0 | Cold | Professional assessment |
| 1–2 | Pro | Competent ally |
| 3–4 | Trusted | Guarded care |
| 5–6 | Devoted | Admitted bond |
| 7–9 | Bonded | Unveiled / oath |

Distress routes to **protective**, not irritated. Graphic intimacy is gated high on the ladder. Test accounts at affection 0 will sound clipped by design.

---

## Memory model

```
TIER 1  Session     Redis        conversation, mood, mission     ~24h TTL
TIER 2  Episodic    Qdrant       summaries + embeddings          permanent
TIER 3  Factual     PostgreSQL   messages, affection, jobs       permanent
```

**Invariant:** chat messages, episodes, affection, and vectors are **SACRED**. Compaction inserts summaries — it does not delete history.

---

## Tech stack

| Layer | Choice |
|---|---|
| API | Python 3.14, FastAPI, uvicorn |
| UI | Flutter Web PWA (`/app/`) |
| Chat model | Local dolphin-mistral (Venice) via gateway |
| Agent model | Local Qwen reasoning distill |
| Images | ComfyUI + Illustrious + Klukai LoRA (leased) |
| Voice | XTTS TTS + Speaches STT on GPU host |
| Bus | RabbitMQ topic + delay buckets + quorum jobs |
| Observability | Prometheus, Grafana, Loki, Tempo, Alloy |

---

## Repository layout

```text
klukai/
├── config/personality.yaml     # lore + speech ladder + moods
├── docker/
│   ├── core/                   # companion-core (app, migrations, tests)
│   ├── events-bridge/          # Redis → AMQP + defer + Her POV worker
│   └── voice/                  # voice container defs
├── flutter_app/                # PWA source (incl. Her POV screen)
├── gateway/                    # nginx
├── ops/dominus-nobara/         # GPU compose, model lock, runbook
├── web-build/                  # release PWA artifacts
├── scripts/e2e_live.py         # live end-to-end suite
├── docs/
│   ├── images/                 # topology diagrams (PNG + SVG)
│   ├── architecture.md
│   ├── homelab-event-bus.md
│   ├── onboarding.md
│   ├── adr/                    # architecture decision records
│   └── runbooks/
└── docker-compose.yml          # core host stack
```

---

## Quick start (operators)

### Prerequisites

- Docker Engine + Compose v2 on the **core host**
- GPU stack healthy on the **GPU host** (see `ops/dominus-nobara/RUNBOOK.md`)
- Private mesh connectivity between hosts
- `.env` with required secrets (never commit it)

### Core host

```bash
cd ~/git/klukai

# env: copy example, fill secrets locally
cp .env.example .env   # then edit — do not commit

docker compose up -d
curl -sf http://127.0.0.1:8300/health

# Web / PWA
./scripts/deploy-web.sh          # preferred
# or: flutter build web --release --base-href=/app/ && rsync into web-build/
```

### Live verification

```bash
# Defaults to the 'claude' test user — refuses jalsarraf without override
python3 scripts/e2e_live.py
python3 scripts/e2e_live.py --only chat
python3 scripts/e2e_live.py --only her-pov
```

### Useful knobs

| Variable | Default | Meaning |
|---|---|---|
| `HER_POV_EXECUTION` | `queue` | `queue` = durable rail; `inline` = in-process (tests / bridge down) |
| `KLUKAI_DISABLE_WARMUP` | unset | Set `1` to disable warm-on-connect |
| `CORE_INTERNAL_TOKEN` | required in prod | Shared secret for bridge → core internal routes |
| `LM_STUDIO_URL` / `LM_STUDIO_TOKEN` | mesh gateway | Local LLM path only |

---

## Security & privacy

- **No cloud chat fallback** — conversations never leave the GPU path by policy.
- Mesh-only publication for inference ports.
- Internal fire endpoints fail closed when the shared token is unset.
- Her POV job status is user-scoped (cross-user → 404).
- Do not log or commit tokens, cookies, or private keys.

---

## Testing

```bash
cd docker/core
python3 -m pytest tests/ -q --tb=short --cov=app --cov-fail-under=95
ruff check app/
```

Live suite (`scripts/e2e_live.py`) exercises HTTP, WebSocket, Postgres, the delay rail, and the full Her POV path against a running stack.

CI uses **self-hosted** runners only (`[self-hosted, unified-all]`).

---

## Documentation map

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Load-bearing structure (this release) |
| [docs/homelab-event-bus.md](docs/homelab-event-bus.md) | Redis → RabbitMQ bridge, jobs, defer |
| [docs/onboarding.md](docs/onboarding.md) | Dev / deploy walkthrough |
| [docs/adr/](docs/adr/) | Decision records (split, GPU lease, voice, …) |
| [docs/runbooks/](docs/runbooks/) | Incident playbooks |
| [ops/dominus-nobara/RUNBOOK.md](ops/dominus-nobara/RUNBOOK.md) | GPU host operations |

---

## Ground rules (agents & humans)

1. Self-hosted CI only — no GitHub-hosted runners.
2. Conventional commits — **never** add AI co-authors.
3. SACRED data is additive only.
4. Test as `claude`, never as the primary Commander account.
5. No second event bus — use `homelab.events` / existing rails.
6. No cloud LLM path — local or fail closed.

---

## License & lore

Personality and speech patterns are original engineering around publicly documented Girls' Frontline 2 character lore. Game assets and trademarks belong to their respective owners.

---

*Built to stay online when he reaches for her — and to keep every word on hardware he owns.*
