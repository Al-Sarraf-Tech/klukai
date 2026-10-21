# Klukai bug scan — 2026-10-21

Requested scope: a deep scan and fixes, with one additional GPT-6.1 Sol agent
at high reasoning. Base commit: `9ee85ca6bf871c7211a1cbc09fbb676a3d202323`.
The extra agent inspected Flutter and companion JavaScript; the primary agent
inspected backend chat, persistence boundaries, API pagination, jobs and builds.
Both reviewed the other side's changes.

## Confirmed defects and corrections

| Area | Reproduction and correction |
| --- | --- |
| Reconnect lifecycle | Calling Flutter `connect()` again left the prior socket or its retry alive. Replacement now closes the old socket and cancels its timer; rejected authentication also prevents resume from redialing. Companion `start()` is idempotent while connected/connecting and handles both authentication close codes. |
| Browser storage | A throwing `localStorage` getter ran before the helper's `try` block. Storage access now happens inside the guarded body. |
| Voice playback ownership | Reusing a message bubble could leave pending synthesis or playback UI attached to another message. Identity changes now cancel owned synthesis, reset voice state, and invalidate stale async completions. The browser playback API cannot stop a clip already playing; later clips are suppressed. |
| Voice gestures | Pointer cancellation invoked the submit callback. Cancellation now invokes `onCancel`; release still submits. |
| Chat history pagination | Timestamp-only cursors skipped rows sharing the page boundary timestamp. The API and client now use `(created_at, id)` and deterministic tie ordering. Invalid cursors return 400 before querying; page size is bounded to 1–200. Existing timestamp-only clients remain supported. |
| Repeated chat text | Reconnect history matching suppressed every historical row sharing a local echo's role/content. Matching is now one-to-one, promotes an eligible completed echo to its canonical server row, and preserves distinct historical rows and active streams. |
| Invalid WebSocket frames | Empty/non-string text and malformed/oversized voice frames could acquire turn ownership and invalidate background extraction. They are rejected before admission. The rejection targets only the submitting device, so it cannot finalize another device's reply. |
| Empty model replies | A clean empty/whitespace response was persisted and mined as a successful exchange. Direct and agent replies now show the existing failure cue and skip assistant persistence/extraction. The user's durable message remains intact. |
| Failed PWA builds | The Make recipe continued after a failed Flutter build, replaced the served bundle with stale output, and reported success. It now stops immediately and preserves the existing bundle on compiler failure. |

Regression tests were run failing before fixes. In particular, a two-device
WebSocket test caught and corrected error broadcasts during review. A browser
test with 50 identical timestamps caught unstable client sorting. A proposed
raw-timestamp field was removed after inspecting the installed Dart runtime and
verifying that it already preserves microseconds.

## Verification

- Core baseline: **6,494 passed**. With fixes: **6,516 passed, 35 skipped,
  59 deselected**, **98.04% coverage**, exceeding the 95% gate. Live integration
  and performance tests were excluded; these runs use mocked external services.
- `ruff check app/ --config ruff.toml`: passed.
- `mypy app/ --config-file mypy.ini`: passed for **173 source files**.
- PostgreSQL 16 proof executed the route's actual SQL against a temporary
  database: 51 same-timestamp messages for Alice plus one for Bob. Alice's pages
  were **50 / 1 / 0**, with no omissions, duplicates, or cross-user rows.
  The fixture retained microseconds. The temporary database was stopped/removed.
- Operations, quality, release metadata and observability: **1,397 passed**
  with the repository-pinned pytest/asyncio/coverage/mutmut tools. Fourteen
  interpreter-identity-sensitive tests failed under that temporary venv; all
  fourteen are covered by a separate **68/68 passing** native-interpreter run
  of `test_light_environment.py`, `test_light_environment_ca.py`, and
  `test_light_environment_inventory.py`. Together these cover all **1,411**
  collected cases, plus four subtests. Initial ambient-tool failures are retained
  in evidence rather than treated as application failures.
- Frontend full suites: **405 VM tests and 214 Chrome tests passed**. Companion
  JavaScript: **159 passed**. A final two-line voice-client ownership correction
  followed those full suites; the entire MessageBubble test file was rerun in
  Chrome: **26 passed**. Final analyzer and format checks passed, and the release
  web build succeeded. Built files remained under `flutter_app/build/web`; the
  served bundle was not replaced.
- Voice runtime unit tests: **33 passed**. Telegram: **36 passed**.
  Events bridge: **28 passed, 3 skipped**.
- Backend Docker build succeeded. All **6 release image runtime checks passed**
  against this new local candidate:
  `sha256:2458f475ec7acefb0ba057a97fb1b38e4261de9d1b5916b0f70666faccaf519d`
  (`klukai-core:deep-scan-20261021`). This is a local test candidate, not a
  CI-certified release.

Local evidence (test logs, failing regressions and PostgreSQL proof script):
`/home/jalsarraf/.local/state/codex-tasks/klukai-deep-scan-20261021/`.

## Runtime and delivery boundary

Read-only health checks on Amarillo reported core, PostgreSQL, Redis and Qdrant
healthy before and after the scan. The running core remained image
`sha256:d6fd652ba1bb4894db0f96bb67cbe3011c5ee1948e965a8a20c301c28d94b21e`,
started `2026-10-21T17:12:40.208852839Z`; it was not restarted or replaced.
No production chat messages, memories, affection, provider accounts or GPU
services were modified or exercised. Provider inference and real-account UI
journeys were not part of verification.

The owner requested a local commit, Amarillo Git mirror sync, GitHub push and
completion email. These fixes are source changes; they have not been deployed.
The pre-existing `web-build/index.html` bootstrap change is unrelated and was
preserved. Release deployment remains subject to the repository's existing
CI/provenance process; the local candidate does not replace it.
