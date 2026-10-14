# Agy chat image deployment plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development task-by-task.

**Goal:** Enable owner-requested Gemini/Agy image generation in authenticated Klukai chat, deploy on Amarillo and merge/push origin/main.
**Architecture:** Existing explicit chat-image pipeline calls an authenticated Unix-socket host worker. The worker uses the proven isolated Agy account runtime, approved references, one generation tool call, persistent request deduplication and no paid-credit/API fallback. Updated owner scope: Agy is now the main image renderer for all paths, with no ComfyUI/GPU fallback; voice remains unchanged.
**Tech Stack:** Existing Python/Pillow/httpx, stdlib Unix HTTP server, SQLite, user systemd, Docker bind mount.
**Spec:** User explicitly selected “Enable Gemini/Agy image generation in chat” and requested deployment/remote merge. This authorizes the production account path, superseding one-off-only scope for explicit chat requests; all no-spending/GPU/other-user privacy rules remain.

## Global constraints

- No new spending: useG1Credits=false, no API-key/ADC provider, no paid fallback or automatic same-provider retry. The later rotation extension permits one recognized service-failure switch.
- Only allowlisted users may use Agy; other users get no image when global Agy is selected. Owner Commander reference only for owner couple requests.
- Send the explicit picture request, locally assembled appearance/outfit tags and selected references. Do not send full chat histories or private archives.
- Default remains disabled on fresh installs. Production global KLUKAI_IMAGE_PROVIDER=agy; zero paid-cloud image allowances remain unchanged. Agy uses the signed-in account with credits disabled.
- No Dominus service change; no desktop credentials in Docker.
- Remote target is existing upstream origin/main; user authorized push. Preserve personal remote.

## Contract

Core settings: KLUKAI_CHAT_IMAGE_PROVIDER=agy; KLUKAI_AGY_ALLOWED_USERS=jalsarraf; KLUKAI_AGY_SOCKET=/run/klukai-agy/image.sock; KLUKAI_AGY_TOKEN_FILE=/run/klukai-agy/token.
Auth: Authorization: Bearer <private file token>. HTTP over Unix socket only.
GET /health -> 200 {"status":"ready","provider":"agy","busy":false}; configuration/liveness, not a fresh Google authentication assertion.
POST /generate -> JSON {request_id:UUID, user_id:string, prompt:string(1..16000), width:int, height:int, include_commander:bool}. Dimensions communicate orientation; native generated dimensions may differ. Only bounded fully decoded PNG bytes are returned with Content-Type:image/png, maximum 16MiB.
Failures: 400 invalid, 401 bad token, 403 unauthorized user/Commander reference, 409 reused/pending request, 429 busy, 502 generation failed, 504 timeout. Bodies contain generic errors, no prompts/tokens/provider logs. Client never retries or falls back.
Host allowed users separately enforced. Persistent SQLite claims request IDs before inference; completed duplicate requests may return cached bytes, never rerender. No automatic retry of interrupted/ambiguous claims.

## Review focus

- User identity cannot select another user's photo; nonallowlisted users fail closed without local rendering.
- Context/history does not enter Google prompt; provider failure does not wake Dominus.
- No reference upload or quota consumption before validated request/auth/claim.
- One call, finite watchdog and exact artifact validation; terminal SUCCESS alone is insufficient.
- New socket directory mount works for UID1000 container and restarts; secrets excluded from git/logs.

### Task 1: Core chat adapter (delegated)

Files: docker/core/app/agy_image_provider.py, background.py, routes.py and related tests/conftest only.
- [x] Write failing tests for selection, private context exclusion, user scope, no fallback, malformed/oversize images and failures.
- [x] Implement client per Contract and route explicit requests before local readiness/locks.
- [x] Run targeted core tests and report exact evidence.

### Task 2: Host worker (root)

Files: scripts/agy_image_worker.py, scripts/agy_art_runtime.py, scripts/agy_art_gate.py, ops/amarillo/tests and systemd unit.
- [x] Write failing tests for auth/validation/idempotency/concurrency/generation lifecycle.
- [x] Implement fixed references, filtered-keyring runtime, generation gate and sanitized output handling.
- [x] Verify with mocked server tests and actual isolated account preflight.

### Task 3: Deploy and integrate (root)

Files: docker-compose.yml, .env.example, docs/handoff, AGENTS.md, README.md.
- [x] Review and test combined changes; build only companion-core.
- [x] Install private runtime/token/reference manifest and user service; enable production main image selector.
- [x] Exercise real core-to-host image path with test account; check owner archive/auth boundaries without mutating owner history.
- [x] Preserve rollback configuration and previous core image; recreate companion-core only.
- [ ] Merge latest origin/main, commit, push origin/main and sync Amarillo; report exact test instructions.

Owner follow-up: “And make it the main version of image generation no more gpu usage.” This expands the initial chat-only request to all image-generation paths. No local image fallback; img2img/live portrait paths must avoid GPU too.

## Owner-requested rotation extension

- Existing Grok account paid usage OFF / hard cap $0 confirmed by owner.
- Native Grok image_edit sandbox, fixed references, readonly native auth, no API-key fallback.
- Persist alternating preferred providers and per-attempt attribution; at most one different-provider service failure fallback.
- Policy/input/refusal/ambiguous timeout fails closed; no GPU image fallback.
- Show owner Commander-and-Klukai samples from both native model providers.

Owner final appearance refinements: slightly lighter light-olive skin and fully anime Commander matching Klukai. Persisted in canonical personality/tags and the shared provider prompt; photo remains an identity reference.
