# Klukai Architecture

> Companion app deployed at `klukai.appnest.cc/app/`. Single-region, two-host topology (amarillo core + dominus voice/GPU). FastAPI + Flutter PWA. This document captures the load-bearing architectural decisions; per-decision ADRs in `docs/adr/`.

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
                                ┌──────────────────────┐
                                │     dominus          │
                                │  voice (CUDA TTS)    │
                                │  ComfyUI (image gen) │
                                │  LM Studio (gemma,   │
                                │  dolphin-24b, etc.)  │
                                └──────────────────────┘
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
| `app/voice_client.py` | TTS shim to dominus voice service |
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

### dominus — GPU sidecar (Tailscale)

Reachable on the Tailnet only. amarillo resolves `dominus` through Tailscale
MagicDNS for GPU APIs and SSH/file transfers; no private-LAN route is required.

- **voice** — TTS service. Container port `8301` periodically loses port binding; remediated by `rm -f && docker compose up` (`feedback_dominus_voice_port.md`).
- **ComfyUI** — image gen. Container maps `8188→8388` per `feedback_comfyui_port.md`.
- **LM Studio** — model server. Hosts dolphin-24b (creative), gpt-oss-20b (JSON-extracting), gemma-4 (annotation/quick). Routing per `feedback_dolphin_for_annotations.md` + `feedback_model_routing.md`. Authenticated via rotating mk_ Bearer (`~/.config/agents/gateway-lmstudio-dominus.token`).
- **Backups** — nightly tar from amarillo over SSH; offsite store on dominus NVMe. Restore drill: `scripts/restore-from-backup.sh`. Reference: `feedback_offsite_backup.md`.

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

## Security

- Secrets at rest: `/etc/credstore.encrypted/klukai-secrets.cred` (TPM2-sealed via `systemd-creds`); `.env` retained for dev only.
- Non-root container runtime; distroless final stage (Phase 4 in progress).
- SHA-pinned deps via `uv pip compile --generate-hashes` (Phase 4 in progress).
- SLSA L2 provenance via `slsa-github-generator` (Phase 4 in progress). Never `actions/attest-build-provenance` (requires paid plan).
- Bearer tokens + per-user rate-limit + admin role; no OAuth.

## Identity rules (absolute)

- **Klukai ≠ Kairi.** Distinct character; distinct DB tables (`klukai_*`) and Qdrant collections. Reference: `feedback_klukai_kairi_separate.md`.
- **Commander is HUMAN.** Male presentation. Never a T-Doll. Reference: `feedback_commander_human.md`.
- **Memory immutability.** Reference: `feedback_never_delete_chat.md`.
- **Speech-pattern routing.** Levels 5-9 must NEVER default to Cold. Regression-guarded by golden tests. Reference: `feedback_speech_routing_bug.md`.

## Non-goals (named so they don't sneak in)

- **No K8s.** Docker compose stays.
- **No multi-region.** amarillo + dominus single failure domain.
- **No OAuth.** Bearer tokens via seed users.
- **No model fine-tuning.** Off-the-shelf models per `feedback_local_llm.md`.
- **No macOS builds** (global CLAUDE.md absolute).
- **No flutter rewrite.** PWA stays.

## See also

- `docs/superpowers/specs/2026-05-16-s-plus-uplift.md` — full S+ uplift spec.
- `docs/slos.md` — per-endpoint SLOs.
- `docs/perf-baseline.md` — perf measurement methodology.
- `docs/runbooks/` — operational playbooks per alert.
- `docs/adr/` — locked decisions with rationale.
