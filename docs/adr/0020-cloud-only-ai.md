# ADR-0020: Cloud-only AI

- **Date:** 2026-10-26
- **Status:** Accepted (owner decision, 2026-10-26)
- **Authors:** jalsarraf
- **Supersedes:** the GPU-host parts of [ADR-0002](0002-amarillo-dominus-split.md),
  the Qdrant tier of [ADR-0003](0003-three-tier-memory.md),
  [ADR-0004](0004-lm-studio-routing.md), the local pipeline of
  [ADR-0006](0006-image-gen-pipeline.md) and [ADR-0007](0007-voice-on-dominus.md)

## Context

Klukai's local AI had shrunk to fallbacks and leftovers:
- chat ran on Sonnet 5.5, with a Dominus model as the secondary;
- background writers could fall back to that model;
- recall used a CPU embedding model on amarillo with Qdrant;
- voice input used the Dominus speech service;
- ComfyUI was a rollback-only option.

Dominus is often off or busy with games. The owner wants a single, predictable
setup that never depends on local GPUs or local models.

## Decision

Klukai uses cloud AI only. No inference of any kind runs for her on
Dominus-Nobara or amarillo: no LLM, no embeddings, no speech recognition and no
image model.

- **Chat:** Claude Sonnet 5.5 (medium effort); Claude Haiku 5.5 answers when
  Sonnet fails or is backing off.
- **Background work:** Claude Haiku 5.5 (high effort) through
  `background_llm`/`extraction_llm`, under one shared daily attempt cap. A
  refused or failed call uses the caller's safe default.
- **Memory recall:** PostgreSQL full-text search finds candidates, and Haiku
  picks the relevant ones. Claude has no embeddings API, so no vectors are
  made. Episodes and exchanges live in PostgreSQL; the Qdrant collections are
  kept untouched but no longer read or written.
- **Voice:** Fish Audio for speech (free model) and speech-to-text
  (`transcribe-1-pro`, pay-as-you-go, owner authorized).
- **Pictures:** the account image worker only; ComfyUI and the GPU lease are
  removed.

The local code paths are removed, not just disabled.
`docker/core/tests/test_cloud_only_invariant.py` fails the build if a local
endpoint, setting or module returns to the core or its compose service.

## Consequences

- Every reply and background call costs Anthropic API usage under the owner's
  standing authorization. Background work and recall share the existing daily
  cap.
- If Anthropic is down she cannot answer; she replies with an in-character
  disruption line, as before.
- Voice input needs Fish credit. Without it, `/api/stt` fails closed.
- Gaming awareness ([ADR-0018](0018-gaming-aware-chat-persona.md)) is dormant:
  its only game signal came from the Dominus gateway, so she now treats him as
  not gaming.
- Animated portrait frames (blink, talk, smile) were drawn only by ComfyUI.
  Cached frames are still served, but no new ones are made.
- Recall quality now depends on full-text candidates. A memory that shares no
  words with the message is only a candidate through its importance and
  recency.
- Design and rollout details:
  [docs/superpowers/specs/2026-10-26-cloud-only-ai-design.md](../superpowers/specs/2026-10-26-cloud-only-ai-design.md).
