# ADR-0007: Voice synthesis on dominus-nobara (RTX 3090 + CUDA)

- **Date:** Origin (formalized 2026-05-16)
- **Updated:** 2026-08-01 (Nobara rebuild and GPU lease)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

Klukai's voice uses the preserved XTTS v2 model and Japanese reference audio.
CUDA inference on the RTX 3090 provides the conversational latency the
companion needs. `amarillo` has an Intel Arc A380 and is not the supported
XTTS execution target.

The former Windows/WSL2 `dominus` deployment is gone. Voice must be restored
without reintroducing a manual container, a LAN dependency, an unbounded model
load, or a root-disk data path.

## Decision

The canonical `companion-voice` service runs in
`ops/dominus-nobara/compose.yaml` and is owned by
`dominus-ai-stack.service`. Its model and mutable cache binds live on
`/mnt/nvmer0`; no retired top-level Compose file owns the service.

`companion-core` reaches the bearer-authenticated endpoint only through
Tailscale at `100.107.121.5:8301` (or the locked MagicDNS name). The published
socket binds only to the Tailnet address.

XTTS loads lazily after acquiring the same restart-safe GPU lease used by
ComfyUI. Acquisition blocks new LLM work, drains/unloads llama.cpp, stops
native vLLM, and verifies quiescence. Release, expiry, or explicit unload
cleans both leased workload classes before removing the marker. Dirty,
expired, or `cleanup_failed` state remains fail-closed until cleanup is
positively verified.

Voice uses a fixed 600-second idle TTL, bounded by the system-wide maximum of
900 seconds. Health checks remain model-free. The game guard stops the voice
container and verifies canonical GPU processes before a game; game end
restores only the empty lazy shell.

## Consequences

- `dominus-nobara` is a single failure domain for voice; chat degrades to text
  when it is unavailable.
- Voice, image generation, local LLMs, and games safely share one GPU through
  explicit arbitration rather than timing delays.
- The first TTS request after idle may pay the XTTS cold-load cost.
- Operators recover the canonical Compose service through the runbook. They
  do not use `docker rm`, publish a second port, or bypass the lease/game guard.
- Voice model bytes and the reference WAV are immutable release artifacts;
  the running container does not download substitutes.

## Alternatives considered

- **CPU-only XTTS on amarillo:** rejected because latency breaks the intended
  conversational pacing.
- **Cloud TTS:** rejected for privacy, cost, and vendor dependency.
- **Arc A380 XTTS:** rejected because the recovered implementation and model
  are validated for the CUDA path.
- **Manual standalone voice container:** rejected because it can bypass the
  RAID, authentication, lease, port, and game invariants.

## Related

- `ops/dominus-nobara/compose.yaml`
- `ops/dominus-nobara/RUNBOOK.md`
- `docker/core/app/voice_client.py`
- `docs/runbooks/voice-unreachable.md`
- ADR-0002 (amarillo/dominus-nobara split)
- ADR-0006 (image generation on the same GPU)
