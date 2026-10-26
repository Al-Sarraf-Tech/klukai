# Klukai on Amarillo: gaming and Fish verification

Owner request: keep Klukai aware of gaming from Amarillo, test the funded Fish account, and verify the UI in Cindy's VM.

## Verified live

- Production remains `klukai-core:verified-617930c-e057a0c029ea`, healthy. No core or PWA release was changed.
- Installed and enabled `klukai-gaming-presence.service` in the owner's user systemd. Its installed executable is `~/.local/bin/klukai-gaming-presence`.
- The service inspects only the owner's process environment for Steam application IDs and a small known-game process-name set. It publishes only a boolean and timestamp, never process details. The file is `config/.amarillo-presence.json`, excluded locally from Git. No model, GPU, network or paid service is used.
- Controlled live test: a short-lived process with a Steam application ID caused the heartbeat to become true; terminating it caused the next heartbeat to become false. This is a signal-path test, not an observation of the owner playing a real game. Non-Steam detection is limited to the listed process names.
- Fish TTS using Klukai's existing in-container credentials returned HTTP 200 and a valid 102442-byte WAV for a short test sentence. Existing free TTS model selection was preserved.
- One Fish STT request, with synthetic speech saying “Klukai can hear me on Amarillo”, returned HTTP 402. The configured API key's account/balance was not inspected, and no key was printed or copied. No retries or topups were attempted. The rejection does not establish which Fish account the owner funded.
- Cindy VM Chrome loaded the public Klukai site and authenticated as the existing `claude` test identity, using a newly minted short-lived test session. No human account credentials, conversations or memories were modified.
- The VM loaded `/app/companion.html`, all four displayed portrait image layers decoded, and a MutationObserver counted four blink closures over 12 seconds after opening the channel. `/api/portrait` returned all six cached frames for the test outfit. This verifies cached animation, not generation of new frames or voice-driven mouth animation.

## Candidate implementation (not deployed)

Branch `fix/amarillo-presence-fish` in Cindy's `workspace/klukai-amarillo-features` worktree:

- `local_presence.py` reads a fresh boolean heartbeat (45-second TTL). Malformed, missing and future/stale records do not assert gaming.
- `LLMRouter.is_game_active` reads an explicitly configured `KLUKAI_GAME_STATUS_FILE`, or the installed default `/config/.amarillo-presence.json` if present, independently of LLM health. Other deployments retain the legacy gateway probe. The host's config directory is already mounted read-only into core.
- Fish STT is explicitly gated by `KLUKAI_FISH_STT_ENABLED=true`, the master voice switch and an existing API key. Default remains off, including this production deployment, because the live test failed.
- REST and WebSocket voice paths both use the cloud transcriber. Base64 is validated and limited to 6 MiB decoded; HTTP is bounded to 30 seconds, with no redirects, retries or local fallback. Fish model header is `transcribe-1-pro`; diarization/audio-event markup are disabled for a plain composer transcript. Provider error details are not exposed.
- Authenticated `/api/voice/capabilities` lets the Flutter microphone follow the server's input switch. Explicit widget overrides remain available for tests. Missing or failed capability discovery keeps the mic hidden. The production app previously hardcoded it off.
- TTS still requires a free-model header; voice letters remain text-only. No local LLM or GPU service was enabled.

## Checks

- 346 focused Python tests passed (Fish, new REST/WebSocket/heartbeat tests, chat handlers and router behavior).
- 62 JavaScript lifecycle tests passed (portrait timers and microphone/voice lifecycle).
- 17 selected Flutter widget tests passed. This is not a full browser test of the candidate PWA.
- Flutter analysis passed on all five changed Dart files; Ruff checks and `git diff --check` passed.
- The VM check above exercised the existing production frontend, not the undeployed candidate.

## Open acceptance and deployment

1. Resolve HTTP 402 for the API account/key Klukai actually uses, then test one short transcription before enabling the STT switch. Do not read, print or transfer credentials or purchase/top up credit.
2. New blink/talk-frame generation remains unimplemented. Existing cached sets work. The old expression renderer requires Dominus ComfyUI; the cloud path currently draws only a base. Restoring new frames needs a bounded, explicit-only cloud image-edit path preserving base alignment, the existing checker and spend ledger. Do not enable automatic/proactive/prewarm generation or restore Dominus GPU dependencies.
3. Core and PWA deployment require the repository's certified CI artifacts and normal rollback-protected release process (`docs/runbooks/release.md`). They cannot be replaced by editing the running container or an unverified image. No GitHub push was made under the owner's standing no-unrequested-GitHub-publishing rule.
4. During release, preserve the existing persistent `LM_STUDIO_URL=http://127.0.0.1:9` override so Klukai never falls back to local LLMs. Keep other companion configuration unchanged. Confirm the next image-pin write preserves that environment override.
5. After the certified release, verify real gaming state reaches chat/proactive behavior, server capability false hides the mic and true reveals it, one user-requested recording transcribes successfully, and failure/cancel leaves typing usable. Verify new portrait-frame generation separately before claiming full restoration.

Only the host heartbeat publisher is installed live. Core/UI changes and new-frame generation are not complete production features.

## Continuation: owner requested completion

The owner subsequently requested “Finish the rest for klukai,” including the previously described release work. The normal CI/release path is being used; no branch protection or artifact verification is disabled.

- A second bounded admission check using Fish's general transcriber returned HTTP 402 with the provider message that API credit is independent of platform credit. No purchase or topup was attempted. The Fish input switch stays off pending API funding by the owner.
- Added explicit `POST /api/portrait/animate` and a companion-stage action to fill only missing blink/talk frames. Cached base images are preserved. Crop-only Seedream edits use inline JPEG sources capped at 42,000 bytes, validated SHA-256 and 512×512 dimensions, authenticated allowlists, fal-only worker routing, existing paid-attempt ledger and the existing Claude image checker. Only face masks are composited back onto the unchanged base.
- Per-frame request identities bind user, outfit, source digest and frame. Pending work is observed through recovery; there is no unjournalled resubmission or automatic re-roll after a blink fails acceptance. Polling, outfit changes and startup remain non-generating.
- Fixed release image pin writes to preserve the owner's environment overrides, including the disconnected local LLM endpoint. This is required before any rollout.
- 469 focused tests passed for the combined Fish, portrait, worker, renderer and release changes. A live isolated-worker rendering check and certified CI are pending at this entry's creation; this paragraph does not claim they passed.
