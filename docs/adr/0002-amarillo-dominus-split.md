# ADR-0002: Compute split — amarillo (core/gateway) + dominus (voice/GPU)

- **Date:** 2026-04 (formalized 2026-05-16)
- **Updated:** 2026-08-01 (Nobara rebuild, leased GPU facade)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

klukai needs three resource classes:

1. **CPU + RAM**: FastAPI core, nginx gateway, PostgreSQL, Redis, Qdrant.
2. **GPU (CUDA)**: voice synthesis (XTTS v2), image generation (ComfyUI
   + Illustrious + Klukai LoRA), and local LLM inference.
3. **External ingress**: Cloudflare-fronted PWA at klukai.appnest.cc.

amarillo is a Linux server (no NVIDIA GPU; Intel Arc A380 for limited
inference). `dominus-nobara` is the Nobara workstation with RTX 3090 (24GB
VRAM) + Tailscale. The former Windows/WSL2 `dominus` installation is gone and
is not a deployment or rollback target.

## Decision

- **amarillo** runs: `companion-core` (FastAPI), `gateway` (nginx),
  `infra-postgres`, `aichat-redis`, `aichat-vector` (Qdrant), and the
  ingest path (Cloudflare → public IP → nginx → core).
- **dominus-nobara** runs the canonical containerized GPU stack:
  `companion-voice` (XTTS v2 CUDA), internal-only `comfyui`, a pinned
  llama.cpp router behind `lmstudio-compat`, and CPU-isolated Speaches.
- Inter-host traffic uses **Tailscale**. Services are addressed through the
  `dominus-nobara` MagicDNS name or locked Tailscale address. This includes
  voice, image generation, LLM
  inference, SSH, backups, and other file transfers.

## Consequences

- klukai has TWO failure domains. dominus down → no voice / no image
  gen, but chat still works (text-only) per
  `docs/runbooks/voice-unreachable.md`.
- Backup strategy spans both hosts: amarillo DBs → dominus tar (per
  ADR-0014).
- Per-port: voice on `:8301`, LM compatibility and the authenticated ComfyUI
  facade on `:1234`, and Speaches on `:8390`. TranscriptionSuite reserves
  internal `:9786` but is hard-disabled and unpublished pending exact gated
  bytes, exclusive GPU admission, and tested inbound authentication. Raw
  ComfyUI `:8188` is internal only and has no host mapping.
- No K8s. Docker compose on each host is the orchestrator.

## Alternatives considered

- **Single-host on dominus-nobara**: rejected — it would couple core state and
  public availability to a gaming GPU workstation. The former Windows/WSL2
  host is not a deployment target (see ADR-0015).
- **GPU compute on amarillo (Arc A380)**: Arc only handles
  inference for small models (gemma-4). Voice + image gen need
  CUDA; the A380 is supplementary, not primary.
- **Cloud GPU (RunPod / Lambda)**: too expensive for a personal
  product and incompatible with offline-local privacy goals.

## Related

- `docs/runbooks/voice-unreachable.md`
- `feedback_dominus_voice_port.md`
- `feedback_comfyui_port.md`
- ADR-0007 (voice on dominus-nobara rationale)
- ADR-0015 (Windows and WSL2 are not deployment targets)
