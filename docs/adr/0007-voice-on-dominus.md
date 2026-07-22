# ADR-0007: Voice synthesis on dominus only (RTX 3090 + CUDA)

- **Date:** Origin (formalized 2026-05-16)
- **Updated:** 2026-07-22 (voice transport moved to Tailscale)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

Klukai's voice is XTTS v2 (Coqui) trained on her Japanese VA's
reference audio. Per `feedback_local_llm.md`: JP voice ONLY (English
voice scrapped — sounded wrong). XTTS v2 generation is GPU-heavy:
~3s of audio takes ~1-2s on RTX 3090 with CUDA. CPU-only inference
is 10-20x slower, breaking the conversational flow.

amarillo has Intel Arc A380 (no CUDA). dominus has RTX 3090.

## Decision

`companion-voice` (XTTS v2 + Piper fallback) runs **only on dominus**
at `dominus:8301`. `companion-core` on amarillo proxies TTS requests
over Tailscale. Per `feedback_dominus_voice_port.md`: the voice
container periodically loses the `:8301` binding (cause unknown,
recurring); fix is `docker rm -f companion-voice && docker compose
-f docker-compose.voice.yml up -d` — see `docs/runbooks/voice-unreachable.md`.

## Consequences

- **Single failure domain for voice**: dominus down = no voice.
  Klukai chat continues text-only (`feedback_dolphin_for_annotations.md`
  notes voice is not blocking the conversation path).
- **No iPhone/local TTS**: rejected — Klukai's voice must be consistent
  across all clients.
- **Tailnet-only path**: Tailscale MagicDNS avoids a hard-coded address, and
  direct peer connectivity keeps voice latency low. `tailscale ping dominus`
  is the canonical transport check.
- **VRAM shares with image gen + LM Studio** on dominus's 24GB RTX 3090.
  Voice gen pre-loads XTTS (~3GB resident); image gen evicts other
  models when running; LLM uses JIT TTL (ADR-0004).
- **Phase 4 circuit breaker** (per `docs/superpowers/specs/2026-05-16-s-plus-uplift.md`
  §5.4) will make voice failure non-blocking: chat returns text-only with
  a "voice unavailable" indicator instead of 5xx.

## Alternatives considered

- **CPU-only XTTS on amarillo**: rejected — 10-20x slowdown breaks
  conversational pacing.
- **Cloud TTS (ElevenLabs)**: rejected — privacy (all dialog is
  Commander-private), cost, vendor lock-in.
- **Voice via amarillo Arc A380**: rejected — XTTS doesn't have
  reliable oneAPI/IPEX bindings. CUDA path is mature.
- **Move voice to amarillo with smaller GPU added**: out of scope;
  dominus already has the right hardware.

## Related

- `docker-compose.voice.yml` (on dominus, not amarillo)
- `app/routes.py:/api/tts`
- `feedback_local_llm.md` (global CLAUDE.md)
- `feedback_dominus_voice_port.md`
- `docs/runbooks/voice-unreachable.md`
- ADR-0002 (amarillo/dominus split)
- ADR-0006 (image gen also on dominus, same GPU)
