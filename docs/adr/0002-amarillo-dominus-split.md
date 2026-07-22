# ADR-0002: Compute split — amarillo (core/gateway) + dominus (voice/GPU)

- **Date:** 2026-04 (formalized 2026-05-16)
- **Updated:** 2026-07-22 (inter-host transport moved to Tailscale)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

klukai needs three resource classes:

1. **CPU + RAM**: FastAPI core, nginx gateway, PostgreSQL, Redis, Qdrant.
2. **GPU (CUDA)**: voice synthesis (XTTS v2), image generation (ComfyUI
   + Illustrious + Klukai LoRA), LLM inference (LM Studio).
3. **External ingress**: Cloudflare-fronted PWA at klukai.appnest.cc.

amarillo is a Linux server (no NVIDIA GPU; Intel Arc A380 for limited
inference). dominus is a Windows workstation with RTX 3090 (24GB
VRAM) + Tailscale.

## Decision

- **amarillo** runs: `companion-core` (FastAPI), `gateway` (nginx),
  `infra-postgres`, `aichat-redis`, `aichat-vector` (Qdrant), and the
  ingest path (Cloudflare → public IP → nginx → core).
- **dominus** runs: `companion-voice` (XTTS v2 CUDA), `comfyui`
  (image gen), LM Studio (gemma-4 + dolphin + gpt-oss).
- Inter-host traffic uses **Tailscale**. Services are addressed through the
  `dominus` MagicDNS name so code and operational commands do not depend on a
  particular `100.x` address. This includes voice, image generation, LLM
  inference, SSH, backups, and other file transfers.

## Consequences

- klukai has TWO failure domains. dominus down → no voice / no image
  gen, but chat still works (text-only) per
  `docs/runbooks/voice-unreachable.md`.
- Backup strategy spans both hosts: amarillo DBs → dominus tar (per
  ADR-0014).
- Per-port: voice on `:8301`, ComfyUI on `:8388` (external) /
  `:8188` (internal — per `feedback_comfyui_port.md`),
  LM Studio on `:1234`.
- No K8s. Docker compose on each host is the orchestrator.

## Alternatives considered

- **Single-host on dominus**: rejected — Windows + WSL2 isn't a
  klukai deployment target (see ADR-0015). PG/Redis on Windows
  bare-metal is unsupported by official images.
- **GPU compute on amarillo (Arc A380)**: Arc only handles
  inference for small models (gemma-4). Voice + image gen need
  CUDA; the A380 is supplementary, not primary.
- **Cloud GPU (RunPod / Lambda)**: too expensive for a personal
  product. Latency adds 100-300ms per round trip vs LAN.

## Related

- `docs/runbooks/voice-unreachable.md`
- `feedback_dominus_voice_port.md`
- `feedback_comfyui_port.md`
- ADR-0007 (voice on dominus rationale)
- ADR-0015 (wsl2 NOT a deployment target)
