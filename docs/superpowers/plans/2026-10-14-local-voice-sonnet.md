# Local GPU Voice and Sonnet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Deploy local Arc XTTS and paid Sonnet medium-thinking interactive chat with Dominus fallback.

**Architecture:** Three independent changes share one isolated worktree: native
voice supervisor, core/client voice jobs, and Anthropic routing. Root integrates
configuration, reviews, builds, deploys, and verifies the release.

**Tech Stack:** FastAPI/httpx/asyncio, PyTorch XPU/TTS, JavaScript, Flutter, Docker, user systemd.

**Spec:** `docs/superpowers/specs/2026-10-14-local-voice-sonnet.md`

## Global constraints

Preserve Dominus gaming guards, five-minute idle behavior, and artifacts.
Use only the existing API key and tested XPU environment. Secrets stay outside
git. CPU/GPU thermal cancellation is 68°C. No GitHub push or hosted CI.

## Review focus

- Provider failure after output must not duplicate answers.
- Tool continuations must preserve call IDs and valid Anthropic message order.
- Cancelling queued or active speech must release admission and native work.
- Foreign job IDs and retained audio must remain private and bounded.
- STT and noninteractive background requests must retain their prior routes.

## Task 1: Native XTTS service

Files: `ops/amarillo/voice_xpu/`, user systemd unit, focused operations tests.
Interface: authenticated `POST /tts` takes text/language and returns JSON
`audio` (base64 WAV), `sample_rate=24000`; `/unload` releases residency.

- [x] Add failing tests for token validation, lifecycle, and worker failure.
- [x] Implement pinned model/reference loading and supervised XPU worker.
- [x] Pass focused tests and static service verification.

## Task 2: Core and playback jobs

Files: `voice_client.py`, `voice_jobs.py`, TTS routes/lifecycle, stage `net.js`,
Flutter TTS helper/playback and focused tests. Preserve LLM-owned files.
Interface: local POST `/api/tts` returns 202 `job_id,status`; GET/DELETE
`/api/tts/jobs/{id}` poll/cancel for the authenticated owner.

- [x] Test bounded admission, ownership, cancellation, and legacy leases.
- [x] Implement one worker and bounded job retention; separate STT URL.
- [x] Add transparent client polling/cancellation with full ordered chunks.
- [x] Pass Python, Node, and Flutter checks.

## Task 3: Sonnet primary

Files: `llm_router.py`, provider helper, `agent_loop.py`, `chat_handlers.py`,
models and focused tests. Environment: `KLUKAI_CLAUDE_PRIMARY=true` and
`ANTHROPIC_API_KEY`, with the latter never committed.

- [x] Test interactive primary, local background, tool translation, and fallback.
- [x] Implement official Messages requests with adaptive thinking/medium effort.
- [x] Preserve actual-model reporting and failure semantics; pass regressions.

## Task 4: Integrate and deploy

- [x] Review code and run combined relevant suites, then fix findings.
- [x] Snapshot current image/env/web metadata and build core plus Flutter.
- [x] Install local voice unit, verify health and core network access.
- [x] Deploy core and web with the authorized API key and local voice settings.
- [x] Run bounded live chat and voice checks, verify unload and Granite continuity.
- [x] Record evidence/rollback, commit, fast-forward main, sync to Amarillo server.
