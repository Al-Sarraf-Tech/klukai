# Handoff: standby is a state she handles

Date: 2026-10-11. Branch `claude/klukai-standby-rqu97r`. For Claude and Codex.
Read `2026-09-25-dominus-tailscale-publish.md` first; nothing here changes its
rules. The repair script, the game-marker gate, the publish list, and the
no-cloud-fallback policy are untouched.

Nothing in this branch has been deployed. Amarillo's `companion-core`, the
PWA in `web-build/`, the Telegram bot, and Dominus's `lmstudio-compat` are
all still running the code from before it. The "Deploying" section says what
each piece needs.

## Why

Powering Dominus off is normal. Seven read-only audits (core chat path,
scheduler, memory/images, Flutter, companion stage, voice, gateway/ops)
traced what actually happened on every surface while she was off, and on the
turn where the gateway came back. The short version: she did not go quiet,
she misbehaved quietly.

- Every turn took the "handler crashed" path: traceback, "something glitched,
  try again", and an orphan user turn appended to the live window per retry.
- On the one turn where the router's availability flag was stale, the
  sentinel ("Communications disrupted, Commander. Standby for reconnection.")
  was stored as her reply, replayed to the model as context, mined for facts
  (mood reset to `composed`, affection streak credited, the outage embedded
  into Qdrant as something she said), spoken aloud by XTTS (spending a GPU
  lease to do it), and pushed as the daily recap / mission debrief.
- Mission mode pushed "...Static on the line. Update delayed." every ~15
  minutes all night.
- The PWA's push-to-talk silently did nothing (STT failure came back as a
  200), and an expired token soft-locked the app on LINK DOWN forever because
  the 4001 close never reaches a client under uvicorn.

## What changed, by commit

| Commit | Area | Verified by |
|---|---|---|
| `f40fbe8` | tgbot: progress actually forwarded, result never throttled away, `--verbose`, stderr drained, one run at a time, Markdown fallback | `make test-tgbot` (36) |
| `b5656c5` | `scripts/s-tier-audit.sh --diff` implemented | shellcheck, manual |
| `37cd0db` | Makefile: `test-tgbot`, `test-ops`, `test-voice`, `test-shell`, `test-all` | — |
| `5821ac5` | companion stage + push-to-talk recorder (`web-build/js` mirrored) | `make test-js` (38 + 20) |
| `fcef395` | Dominus gateway: bearer required for GPU control, bounded inference wait, 64 MiB bodies, non-ASCII bearer → 401, dockerd drop-in `-` prefix | gateway suite 90, `make test-ops` |
| `39f7f7a` | proactive rails: mission ticks, quiet hours, local day boundaries, push off-loop, brief hold, `_deliver` honours False | core suite |
| `f51fa54` | Her POV stuck jobs, `parse_json`, archive fail-closed, orphan files, MCP re-init, lease budget, Stripe rotation, voice-service STT off the event loop | core suite, `make test-voice` (30) |
| `fd757ed` | Flutter PWA: 401 → login, images, frozen bubbles, resume, lock reason, voice timeouts | `flutter analyze`, `flutter test` (229 VM, 286 Chrome) |
| `04218ed` | core chat path: in-band standby, nothing recorded or mined from a non-answer, voice path, goodbye false positives, recap on local day | core suite 4616, coverage 97.2%, ruff, mypy |

Full detail per change is in `CHANGELOG.md` under "Fixed — standby hardening".

### Refactors (behaviour-preserving)

- `app/proactive/engine.py` 679 → 472 LOC. The APScheduler job table
  (`_register_jobs`, `_cron`), the thin scheduled adapters
  (`_command_decision_check`, `_portrait_backfill`, `_deferred_sweep`) and
  `_catch_up_missed` moved to `app/proactive/schedule.py` as `ScheduleMixin`,
  the same pattern as the other mixins. `events._random_event` and
  `_dream_event` each gained a `_..._blocked()` guard helper; order is
  unchanged (the goodbye-hold check has a side effect and the game probe is
  the only await, so it stays last). Verified: full suite, coverage 97.2%,
  ruff, mypy, radon.
- `chat_handlers._handle_message` radon F (82) → B (6). It now reads as the
  phase pipeline it always was: `_handle_mission_commands` →
  `_record_user_turn` → `_build_system_prompt` (which composes
  `_recall_context`, `_her_day_for_turn`, `_presence_block`,
  `_goodbye_block`, the dream/image hints, and appends the per-message
  blocks in the original order) → `_build_messages` → `_agent_reply` or
  `_stream_reply`/`_stream_tokens` → the no-answer check →
  `_record_assistant_turn` → `_apply_memory_overrides` →
  `_schedule_followups`. `_Reply` carries text/model/latency/stream_failed
  between phases. Every `patch("app.chat_handlers.X")` target in the tests
  still resolves at the same path, and the local imports that tests patch
  through `app.helpers` stayed local.
- `background.background_extraction` F (70) → B. Phases:
  `_apply_memory_curation`, `_store_extracted_facts`,
  `_store_gift_and_inside_joke`, `_store_promises`, `op_brief.on_turn`,
  `_resolve_mood`, `_apply_mood` (captures `prev_mood` before
  `set_last_mood`; `_maybe_auto_start_mission`), `_apply_affection`
  (`_announce_level_change`), `_normalize_topics`, `_store_exchange`,
  `_maybe_store_episode`. Order unchanged. `HIGH_INTENSITY_MOODS` is now a
  module constant (a test greps the source for the gate line).
- Remaining D-rated functions, if you want to keep going: `image_gen._inpaint_set_inner`
  (26), `wardrobe.pick_daily_outfit` (27), `agent_loop.AgentLoop.run` (30),
  `reflect_helpers._maybe_reflect_on_return` (22).

## Decisions made here that you may want to revisit

1. **The unanswered user turn is dropped from the live session** when she has
   no answer (`chat_handlers._drop_unanswered_turn`). PostgreSQL keeps every
   message; only the 16-turn window replayed to the model is affected. The
   alternative (keep it as "what he said while she was off") leaves
   consecutive user turns, which the Mistral template rejects and other
   models answer as one lump, and retries evict real history. When the GPU
   comes back she resumes from the last real exchange and answers what he
   re-sends with clean context.
2. **`_handle_message` now returns her reply (`str | None`)** and the voice
   path speaks the return value instead of re-reading the session. Any new
   caller must handle `None` as "no answer this turn".
3. **Anniversary remark moved 07:58 → 08:00** because it now goes through
   `_can_send()`, which refuses anything inside quiet hours. It shares the
   minute with the morning greeting; within one minute, order is whichever
   finishes first.
4. **The tailnet token-less bypass on the gateway is narrowed** to `/v1/*`,
   `GET /api/v0/models` and `GET /api/v1/models`. `aichat` keeps working
   (that is all it uses); anything that changes GPU state needs the bearer
   from every source. companion-core already sends it everywhere. If some
   other token-less client on the tailnet loads/unloads models, it now gets a
   401 — give it the token rather than widening the bypass.
5. **Mission auto-start has no expiry.** A mission started from a
   `battle_ready` mood ends only on the explicit cancel phrase or engine
   shutdown, and its ticks bypass `_can_send()`. With the GPU off the ticks
   now skip silently instead of spamming, which removes the acute symptom,
   but a ceiling (or routing `_fire_update` through `_can_send()`) is still
   worth deciding. Not done here because it changes behaviour for explicitly
   started missions too.
6. **Redis and Qdrant are published unauthenticated** on both overlays by
   Amarillo's nginx stream proxies (`gateway/nginx.conf:50-67`). Auth lives in
   the aichat compose outside this repo; dropping the ZeroTier binds is a
   network-policy call. Flagged, not changed.

## Deploying

- **companion-core (amarillo):** `docker compose build companion-core &&
  docker compose up -d companion-core`. The events bridge changed too
  (`JOB_RUN_TIMEOUT` 600→900): `docker compose up -d --build events-bridge`.
  No migration. Do not restart core just because Dominus is off.
- **PWA:** `scripts/deploy-web.sh` (needs Flutter on amarillo). It rebuilds
  from `flutter_app/` and rewrites `web-build/`; the hand-mirrored
  `web-build/js/audio_recorder.js` is already updated in git and the build
  copies `flutter_app/web/js/` over it identically.
- **Telegram bot:** `systemctl --user restart klukai-bot.service` on the host
  that runs it (its venv needs no new packages).
- **Dominus gateway (`lmstudio-compat`):** this is a guarded promotion, not a
  hot fix. Follow RUNBOOK "build local images" and the source-promotion steps
  (the live source is the RAID tree, not this checkout). Recreating
  `lmstudio-compat` is allowed by the repair script's own list; do not touch
  `llama-router` or `comfyui`, and do not do it while the game marker exists.
  `compose.yaml` gained `mem_limit: 512m` / `pids_limit: 256` on that one
  service; the compose contract test pins them. `docker.service.d/20-tailscale-address.conf`
  changed one character (`ExecStartPre=-`): install it with `daemon-reload`
  only, as before — do not restart Docker to prove it.
- **Voice service (`companion-voice` on Dominus):** same promotion path. The
  change is STT-only (decode off the event loop, 8 MB cap, temp-file cleanup).

## What to check after deploying

1. With Dominus **off**: send a chat message. Expect one bubble with the
   sentinel, `done` with model `offline`, composer unlocked, no traceback in
   `docker compose logs companion-core`, and `companion_messages` gains the
   user row but no assistant row. Send a second message; the live session
   must not accumulate user turns (`redis-cli --scan --pattern 'session:*'`
   and inspect `turns`).
2. With Dominus **off**, hold push-to-talk: expect the "// VOICE LINK
   GARBLED" toast within 35 s, not silence. Tap a speaker icon: expect the
   "// VOICE SYNTH OFFLINE" toast, not a spinner.
3. Power Dominus **on** (normal boot; let the publish repair do its work per
   the 2026-09-25 handoff). The next message must be answered normally, with
   no sentinel in her context. `GET /health` on `:1234` now also reports
   `inference_busy`.
4. Start a game. The first chat turn after the marker appears should route to
   the gaming model on the next turn rather than taking the gateway down for
   15 s (watch for "gateway stays available" in core logs).
5. Leave the companion stage open across a phone lock with the music off;
   she must still speak afterwards.
6. Overnight with Dominus off: no "Static on the line" pushes, no lore pings
   after 22:45, no dream push after a "goodnight".
7. `scripts/s-tier-audit.sh --json > docs/s-tier-audit-last.json` once on a
   known-good tree, then `--diff` on later runs.

## Test commands used here

```bash
make test-local            # docker/core: 4616 passed, coverage 97.2%
make lint-local type-check # ruff, mypy clean
make test-tgbot test-ops test-voice test-js test-shell
cd flutter_app && flutter analyze && flutter test
cd flutter_app && CHROME_EXECUTABLE=... flutter test --platform chrome
cd ops/dominus-nobara/lmstudio-compat && python -m pytest tests   # 90
```

The Flutter browser suites (`@TestOn('browser')`) need Chrome; the
`platform/browser_test.dart` VM-stub test is now tagged `vm` so it is skipped
there. The gateway suite needs its own pins (`requirements-test.lock`,
starlette 1.3.1), which conflict with core's — use a separate venv.
