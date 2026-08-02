# Klukai Architecture

> Companion app deployed at `klukai.appnest.cc/app/`. Single-region, two-host topology (amarillo core + dominus-nobara voice/GPU). FastAPI + Flutter PWA. This document captures the load-bearing architectural decisions; per-decision ADRs in `docs/adr/`.

## Topology

```
                                ┌──────────────────────┐
                                │  Cloudflare (TLS)   │
                                └──────────┬───────────┘
                                           │
                                ┌──────────┴───────────┐
                                │  nginx gateway       │ amarillo
                                │  (loopback only)     │
                                └──────────┬───────────┘
                                           │
                                ┌──────────┴───────────┐
                                │  companion-core      │ FastAPI
                                │  Python 3.13         │
                                └──┬─────┬─────┬───────┘
                                   │     │     │
                  ┌────────────────┘     │     └─────────────┐
                  │                      │                   │
            ┌─────┴──────┐      ┌────────┴─────┐    ┌────────┴──────┐
            │ PostgreSQL │      │   Qdrant     │    │     Redis     │
            │  factual   │      │   vector     │    │   sessions    │
            └────────────┘      └──────────────┘    └───────────────┘

                                           │ Tailscale
                                           ▼
                                ┌──────────────────────────┐
                                │     dominus-nobara      │
                                │ lmstudio-compat :1234   │
                                │ llama-router (internal) │
                                │ voice :8301             │
                                │ ComfyUI (internal only) │
                                │ speech :8390            │
                                │ transcription disabled  │
                                └──────────────────────────┘
```

## Layers

### Frontend — Flutter PWA

- **Hosting:** `klukai.appnest.cc/app/` (Cloudflare → nginx gateway → static bundle).
- **Build flag:** `--base-href=/app/` ABSOLUTE; without it the service worker intercepts the login page (`feedback_flutter_base_href.md`).
- **Image auth:** `Image.network(headers:)` ignored on web — uses `http.get` + `Image.memory` (`feedback_flutter_web_image_auth.md`).
- **Service worker scope:** `/app/`; pinned to prevent SW from caching the auth gateway response.

### Gateway — nginx

- Reverse-proxies `companion-core` on loopback. TLS terminated by Cloudflare. Caches `/app/` static with `max-age=60` to avoid stale-shell pain (`feedback_cloudflare_cache.md`).
- Per-route auth: bearer-token gate at `/api/*` (handled by FastAPI). Static under `/app/` is unauthenticated by design.

### companion-core — FastAPI service

Single FastAPI app. Major modules:

| Module | Responsibility |
|---|---|
| `app/main.py` | Lifespan, OTel init, FastAPI app construction |
| `app/routes.py` (→ `app/routes/`) | HTTP API surface; split into per-group routers |
| `app/chat.py` | Message pipeline, WebSocket handler |
| `app/personality/` | Speech patterns, moods, system-prompt assembly |
| `app/affection.py` | Score progression, level transitions |
| `app/memory.py` | Three-tier memory orchestration |
| `app/memory_archive.py` | Curated photo album (gpt-oss-20b selects, dolphin-24b annotates) |
| `app/proactive.py` (→ `app/proactive/`) | Scheduled messages, mission timers, decompression |
| `app/image_gen.py` | ComfyUI + Illustrious + Klukai LoRA pipeline |
| `app/voice_client.py` | TTS shim to dominus-nobara voice service |
| `app/llm_router.py` | Tier routing: gemma (quick) / dolphin (creative) / gpt-oss (JSON) |
| `app/audit_chain.py` | HMAC-tamper-detected audit log |
| `app/observability/` | OTel tracing, Prom metrics, structured logging |
| `app/billing.py` | Dormant scaffold (personal-use; subscription tiers + Stripe checkout) |
| `app/auth.py` | Bearer tokens, per-user rate-limit, admin role |
| `app/ws_manager.py` | Multi-device WebSocket connection tracking |

Public-API entry points: `app.routes:register_all(app)` registers per-group routers in `app/routes/`.

### Three-tier memory

```
TIER 1 — Session (Redis)       TTL 24h   conversation + mood + mission state
TIER 2 — Episodic (Qdrant)     ∞         summaries with embeddings (nomic 768-dim)
TIER 3 — Factual (PostgreSQL)  ∞         messages, affection, relationship facts, audit chain
```

Tier-promotion pipeline:
1. Inbound message → Redis (session).
2. Background compaction (`app/background.py`) summarizes session windows → Qdrant embedding + PG row.
3. Memory archive curator (`app/memory_archive.py`) selects high-affection moments → annotated journal entry + ComfyUI image.
4. Daily reflection (`app/proactive/reflections.py`) reads PG + Qdrant → writes back a journal entry.

**Memory integrity invariant:** chat messages, episodes, affection rows, Qdrant vectors are **SACRED**. Compaction summarizes (insert new) — never deletes. Migration changes always backfill from old format. Reference: `feedback_never_delete_chat.md`.

### dominus-nobara — containerized GPU sidecar (Tailscale)

The canonical deployment is `ops/dominus-nobara/compose.yaml`, managed by the
`dominus-ai-stack.service` user unit. The host owns only Docker, the NVIDIA
driver/container runtime, Tailscale, systemd, and RAID mounts. Application
runtimes, CUDA user space, and service dependencies remain in containers except
for the preserved native-vLLM virtual environment, which is self-contained at
`/mnt/nvmer0/ai/vllm` and does not modify the OS Python. Canonical container
models, caches, state, logs, and outputs live below
`/mnt/nvmer0/services/ai-stack`; preserved native-vLLM model bytes remain under
`/mnt/nvmer0/models`. Both locations are on the NVMe RAID, and the stack refuses
to start if that mount is absent.

Every published socket binds to the target's Tailscale address
`100.107.121.5`, reachable through that address or
`dominus-nobara.tail9bdca.ts.net`. No service binds a host port on `0.0.0.0`,
and private-LAN addresses are not fallbacks.

| Compose service | Tailnet endpoint | Responsibility |
| --- | --- | --- |
| `lmstudio-compat` | `:1234` | CPU-only, bearer-authenticated LM Studio/OpenAI compatibility and locked catalog |
| `llama-router` | internal `:8080` | Pinned llama.cpp router; lazy model loading with one preset maximum |
| `companion-voice` | `:8301` | Lazy XTTS v2 and Whisper voice service; bearer-authenticated work endpoints |
| `comfyui` | internal `:8188`, via the authenticated `:1234/api/v1/comfy` facade | Offline image generation from the read-only model release and bounded GPU lease |
| `speaches` | `:8390` | Authenticated, offline CPU speech API; 600s default and hard 895s timer cutoff under the 900s policy; no NVIDIA device |
| `transcriptionsuite` | reserved internal `:9786`; no host endpoint | Recovery definition; hard-disabled with no GPU until exact model, exclusive interlock, and inbound-auth gates land |

`transcriptionsuite-bootstrap` is also hard-disabled and receives no GPU;
`hf-cache-materialize` is the only available one-shot maintenance job and has
no network. Neither is a persistent API service or published endpoint.

Client-facing LLM names and environment variables retain their historical
`LM_STUDIO_*` spelling for compatibility, but no LM Studio desktop/server
process is part of this stack. `lmstudio-compat` forwards inference to
`llama-router`, which loads only aliases in `models.lock.json`. LLM residency
has a hard maximum of 900 idle seconds; the llama.cpp timer runs slightly
earlier to account for its polling interval, and the gateway removes client
TTL overrides.

GameMode creates `/run/user/1000/dominus-gpu/game-active` before stopping GPU
containers. While it exists, the complete canonical Compose user unit is
stopped; the independent native vLLM proxy remains only to reject work with
HTTP 503. Game exit starts the canonical unit through its preflights, restores
empty/lazy service shells, and never preloads a model.
The start hook also checks NVIDIA's live compute-process inventory after the
container/systemd stops and fails closed if any canonical AI process remains.

## Observability

Per ADR-0017 (S+ Phase 2):

```
companion-core ──OTLP──> alloy ──> Prometheus  (metrics; 15d)
                              ├──> Loki        (logs; 30d, structured + trace_id)
                              └──> Tempo       (traces; 7d)
                                     │
                                     ▼
                                  Grafana
```

- All four observability containers bound to loopback; access via Tailscale + Cloudflare.
- Volumes on `/mnt/nvmeINT/obs/klukai/`.
- 13 alerts in `docs/alerts/`, each with `runbook_url` linking to `docs/runbooks/`.
- Dashboards-as-code in `docs/dashboards/` (PR-reviewed).

## Build & deploy

- **CI:** `.github/workflows/ci.yml` — ruff + mypy + bandit + safety + pytest + trivy. 5-stage gate, all must pass.
- **Release:** `.github/workflows/release.yml` — syft SBOM + cosign keyless sign + image push.
- **Nightly:** `.github/workflows/nightly.yml` — mutmut + perf-baseline collection.
- **Renovate:** weekly batch on Python deps + Dockerfile bases + GHA actions; auto-merge security only.
- **Conventional commits + git-cliff:** `cliff.toml` generates CHANGELOG slices per tag.
- **GPU stack:** `ops/dominus-nobara/compose.yaml` is the only deployable
  Compose definition for target AI services. Its user unit starts
  `llama-router`, `lmstudio-compat`, `companion-voice`, `comfyui`, and the
  CPU-only `speaches`; both TranscriptionSuite definitions remain hard-disabled.
- **Model releases:** `models.lock.json` plus SHA-256 verification defines the
  immutable model set. Containers operate offline and mount releases
  read-only; writable service caches are separate verified views.

## Security

- Secrets at rest: `/etc/credstore.encrypted/klukai-secrets.cred` (TPM2-sealed via `systemd-creds`); `.env` retained for dev only.
- Non-root container runtime; distroless final stage (Phase 4 in progress).
- SHA-pinned deps via `uv pip compile --generate-hashes` (Phase 4 in progress).
- SLSA L2 provenance via `slsa-github-generator` (Phase 4 in progress). Never `actions/attest-build-provenance` (requires paid plan).
- Bearer tokens + per-user rate-limit + admin role; no OAuth.
- The Nobara gateway, voice, and speech APIs use independently rotated bearer
  credentials from `/mnt/nvmer0/services/ai-stack/config/stack.env` (mode
  `0600`). Never recover credentials from old logs.
- Tailnet-only host bindings are mandatory for published ports 1234, 8301, and
  8390. Port 9786 is reserved inside the disabled container network and must
  have no host binding. A raw ComfyUI host binding is forbidden.

## Identity rules (absolute)

- **Klukai ≠ Kairi.** Distinct character; distinct DB tables (`klukai_*`) and Qdrant collections. Reference: `feedback_klukai_kairi_separate.md`.
- **Commander is HUMAN.** Male presentation. Never a T-Doll. Reference: `feedback_commander_human.md`.
- **Memory immutability.** Reference: `feedback_never_delete_chat.md`.
- **Speech-pattern routing.** Levels 5-9 must NEVER default to Cold. Regression-guarded by golden tests. Reference: `feedback_speech_routing_bug.md`.

## Non-goals (named so they don't sneak in)

- **No K8s.** Docker compose stays.
- **No multi-region.** amarillo + dominus-nobara single failure domain.
- **No OAuth.** Bearer tokens via seed users.
- **No model fine-tuning.** Off-the-shelf models per `feedback_local_llm.md`.
- **No OS-installed AI runtimes.** GPU applications and their user-space
  dependencies stay in the canonical containers; the preserved native-vLLM
  RAID virtual environment is the explicit compatibility exception.
- **No Windows/WSL2 rollback.** The lost `dominus` install is historical
  evidence only; rollback uses a verified Nobara release/config/image set.
- **No macOS builds** (global CLAUDE.md absolute).
- **No flutter rewrite.** PWA stays.

## See also

- `docs/superpowers/specs/2026-05-16-s-plus-uplift.md` — full S+ uplift spec.
- `docs/slos.md` — per-endpoint SLOs.
- `docs/perf-baseline.md` — perf measurement methodology.
- `docs/runbooks/` — operational playbooks per alert.
- `docs/adr/` — locked decisions with rationale.
