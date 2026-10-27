# Klukai on cloud AI only — design

- **Date:** 2026-10-26
- **Status:** Approved by the owner (2026-10-26)
- **Supersedes:** the Dominus-local parts of ADR-0004 (local model routing through the
  compatibility gateway), the "No cloud LLM fallback" note in `CLAUDE.md`, and the
  Dominus voice/ComfyUI notes in `AGENTS.md`.

## Owner decisions (2026-10-26)

1. Klukai uses cloud AI only. No inference of any kind runs on Dominus-Nobara or on
   Amarillo for her: no LLM, no embeddings, no speech recognition, no image model.
2. Interactive chat stays on Claude Sonnet 5.5. Every background model call uses Claude
   Haiku 5.5 at high effort (the existing `extraction_llm` route).
3. Memory recall stops using embeddings. Claude has no embeddings API, so recall becomes
   "Postgres candidates, then Claude Haiku (high effort) picks the relevant ones".
4. Voice input uses Fish Audio speech-to-text (`transcribe-1-pro`, pay-as-you-go, owner
   authorized). Voice output stays on Fish Audio TTS.
5. Every Dominus/local code path is removed, not just disabled.

## Current state (production at 617930c)

| Function | Today |
|---|---|
| Interactive chat | Sonnet 5.5; on failure, Dominus `lmstudio-compat` (deployment points it at a dead address) |
| Background text/JSON | Haiku 5.5 high via `background_llm`; on failure, the caller's local request, then defaults |
| Agent loop outside chat | `complete_local` (Dominus only) |
| Memory recall | nomic-embed-text v1.5 on `aichat-inference` (CPU, Amarillo) + Qdrant |
| Voice input | Dominus companion-voice `/stt` |
| Voice output | Fish Audio TTS (Dominus voice as an alternative backend) |
| Pictures | Cloud providers; ComfyUI on Dominus only when `KLUKAI_IMAGE_PROVIDER=comfyui` |

## Design

### Language models (`llm_router`, `llm_completions`)

- Two models, both on the Anthropic Messages API with the existing key:
  `claude-sonnet-5-5` (medium effort) for interactive chat and
  `claude-haiku-5-5` (high effort) for everything else.
- `route()`: interactive and Sonnet ready → Sonnet; otherwise Haiku. No key → a clear
  configuration error instead of a local route.
- `stream()` / `complete_chat()`: a Sonnet failure before any token falls back to Haiku
  once; a Haiku failure yields `FAILURE_SENTINEL` (stream) or raises (complete_chat).
- The non-interactive agent loop uses `complete_chat` with the Haiku config.
- Removed: `LM_STUDIO_URL`, the LM Studio availability probe, the gateway game-active check, the
  global LM gate, `complete_local`, `_stream_lmstudio`, `keepalive`, the `LOCAL_*` model
  aliases and `app/lm_gateway.py`.
- Gaming awareness reads Amarillo's fresh boolean heartbeat, independently of model availability.

### Background route (`background_llm`, `extraction_llm`)

- `background_llm.text()` / `json_object()` call Haiku only. Unavailable, capped or
  failed → the caller's safe default. The `local=` fallback argument and every local
  request built for it are deleted from the callers.
- `extraction_llm` always uses Anthropic; `KLUKAI_EXTRACTION_PROVIDER` is no longer read.
  The shared daily ledger (`KLUKAI_EXTRACTION_DAILY_LIMIT`) still caps background calls.

### Memory recall without embeddings (`memory`, `memory_exchange`)

- Storing a memory writes only the Postgres row (summary, keywords, emotion tags,
  importance). No vector, no Qdrant point.
- Recall: Postgres returns up to 40 candidates for the user, ranked by full-text match on
  summary and keywords, then importance and recency. Claude Haiku (high effort, through
  `background_llm.json_object`) returns the ids of the most relevant ones, in order.
  If Haiku is unavailable, the Postgres ranking is used as is.
- Message-level search follows the same pattern.
- Existing Qdrant collections are left in place, unused, for rollback; nothing is deleted.
- Removed: `embed_text`, the embedding cache, `INFERENCE_URL`, Qdrant writes and reads.

### Voice

- Speech-to-text: `POST https://api.fish.audio/v1/asr` with header `model:
  transcribe-1-pro`, `Authorization: Bearer $FISH_API_KEY`, multipart audio. The existing
  STT route keeps its request/response contract.
- Text-to-speech: Fish Audio only.
- Removed: the Dominus voice backend (`VOICE_URL`, `VOICE_STT_URL`, `/stt` on 8301), local
  voice unload and GPU-lease capability headers.

### Pictures

- Removed: the ComfyUI provider (`image_comfy`), the Dominus GPU lease (`gpu_lease`,
  `image_leases`) and every ComfyUI-only path (`COMFYUI_URL`). Cloud providers unchanged.
- Explicit portrait animation retains CPU crop/mask/composite operations around cloud
  edits. These contain no local model and never submit automatic pictures.

### Health, warmup, observability

- Health reports the cloud dependencies (Anthropic configured, Fish configured) and drops
  LM Studio, embeddings, Dominus voice and ComfyUI checks. Model warmup is removed.

### Deployment (`docker-compose.yml`)

- `companion-core` no longer receives `LM_STUDIO_URL`, `LM_STUDIO_TOKEN`, `COMFYUI_URL`,
  `VOICE_URL`, `VOICE_STT_URL`, `INFERENCE_URL` or `KLUKAI_EXTRACTION_PROVIDER`.

## Invariant and verification

- A test fails if any module under `docker/core/app` references the Dominus Tailscale
  address, `LM_STUDIO`, `INFERENCE_URL`, `VOICE_STT_URL`, `COMFYUI_URL` or Qdrant.
- After deploy: the running container has none of those variables; Dominus
  `lmstudio-compat` and companion-voice, and Amarillo `aichat-inference`, receive no
  requests from Klukai; one chat turn, one background call, one memory recall and one
  voice transcription succeed on the cloud providers.

## Dominus retirement (owner continuation, 2026-10-26)

- All Klukai CI profiles and releases use Amarillo, regardless of an old heartbeat.
- Stop the dedicated Dominus CI pool and exclude Klukai from its shared UCI pool.
- Retire the old AI containers and startup/repair hooks, preserving data and models.
- Stop the cross-host evidence pull and archive only Amarillo's CI records.

## Out of scope

- Re-pointing unrelated fleet bus triage.
- Re-embedding or deleting old Qdrant data.
