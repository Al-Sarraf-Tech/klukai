# Responsiveness implementation plan

**Goal:** Make chat and ordinary UI interaction feel faster by removing avoidable application work.

**Design:** Preserve Haiku xhigh, validated buffering, cloud-only providers, Fish voice, durable chat history and all provider budgets. Deliver an already completed tool answer promptly; overlap independent prompt reads; reuse parsed Markdown during screen rebuilds. Prefer these measured code-path improvements over changing model quality or skipping memory. Work in `.worktrees/responsiveness-20261103` on `codex/responsiveness-20261103`; the production checkout is deployer-managed and its original state was restored after moving task edits. Leave unrelated untracked files untouched. No credential reads, production conversation mutation, or live inference for benchmarks.

**Tech stack:** Python asyncio/FastAPI and Flutter web.

## Tasks

- [x] Remove artificial tool-answer pacing in `docker/core/app/chat_reply.py`. Test complete text, failed replies, completion ordering and cancellation. Benchmark delivery using synthetic completed answers before and after.
- [x] Move independent history/presence reads into prompt fan-out in `docker/core/app/chat_prompt.py`. Test overlap, exact prompt order/stable prefix, failures and cancellation. Keep decision effects before world reads.
- [x] Cache unchanged Markdown parsing during Flutter rebuilds. Verify changed content, final text, errors and disposal; run analyzer and affected widget tests.
- [x] Run relevant backend regression suites and cloud-only invariants, frontend tests and release build; independently review changes. Record reproducible measurements and their limits.

## Review focus

Completion delivery must still preserve canonical replies on disconnect. Failed model output must not enter memory. Concurrent prompt reads must remain scoped to one user, retain fallback behavior and be joined on cancellation. Markdown cache must invalidate on text changes and remain bounded by widget lifetime. Flutter already coalesces widget rebuilds within a frame; token mutation and scroll scheduling are unchanged. Final canonical text, error and disposal behavior remain covered by existing browser tests.

## Evidence

Baseline checkout: `82d4f0e0e85fad4f26a315c1b5a4b387c408b775`. Running core observed 2026-11-03: image `klukai-core:verified-50b2707-543c4fe10386`, container `a8fee68ce1b9`, started `2026-11-02T03:30:33.91139839Z`, healthy. Logs contain provider token counts but not useful per-phase latency; static findings and deterministic offline benchmarks establish application overhead, not cloud latency improvements.

Prompt benchmark: loaded the baseline `ChatPromptBuilder` from `git show 82d4f0e:docker/core/app/chat_prompt.py` alongside the changed class, using the offline fixture in `tests/test_chat_prompt_parallel.py`. Each run supplied identical synthetic data and delayed main recall, recent assistant history and presence by 30 ms each with `asyncio.sleep`; all other dependencies were mocked. Seven runs per version measured the full `build()` with `time.perf_counter()`: baseline median **91.20 ms**, changed median **30.90 ms**, a **60.30 ms** reduction in this controlled case. Full prompt text and cached persona prefix were identical. This demonstrates overlapping existing waits; it does not measure production database or cloud response times. Regression tests use event barriers rather than latency thresholds to prove overlap and cancellation cleanup, and cover stable prompt order, user scoping, echo filtering and independent lookup failures.


Tool-reply benchmark: isolated `ChatReplyRuntime.agent()` with an `AsyncMock` agent returning a completed synthetic answer and an `AsyncMock` websocket. One `time.perf_counter()` sample per length, no provider, database, or network calls. Baseline was measured before editing the same module; model generation time is excluded.

| Characters | Before delivery (ms) | After delivery (ms) | Token frames before → after |
| --- | ---: | ---: | ---: |
| 200 | 505.33 | 0.87 | 25 → 1 |
| 1,000 | 2,524.32 | 0.57 | 125 → 1 |
| 3,000 | 7,577.25 | 0.57 | 375 → 1 |

The regression is `tests/test_agent_reply_delivery.py`; before the change, the long reply failed its single-frame assertion. Existing reconnect tests now explicitly verify a late device that misses the sole agent frame recovers the complete answer from `done.text`.

Browser parser fixture: eight mounted messages, initial render plus twenty parent rebuilds. `message_content_test.dart` counts calls at the real Dart-to-JavaScript parser boundary: **168 → 8 calls (95.2% fewer)**. Text changes and canonical replacements invalidate the widget-local cache; remounting parses afresh. This is a work-count measurement, not a frame-rate or live page-latency claim.

Verification so far: offline core suite `python3 -m pytest tests/ -q --tb=short -m 'not integration and not perf'`: **7,350 passed, 71 skipped, 59 deselected**. Additional prompt failure-default cases passed after that suite started. Focused initial aggregate: 291 passed. Changed backend sources pass Ruff and mypy (`--follow-imports=silent`). The touched preexisting shared test harness has three F841 warnings identical at baseline; no new lint violations. No live chat, voice or image requests were made, no conversation records were modified, and no deployment was performed.

Independent code review found no actionable regressions and reran 37 focused tests successfully. Checked synchronous parser-script ordering before Flutter startup to rule out a cache initialization race.

Final frontend verification (2026-11-03): the repository's test selector and strict machine-event validator confirmed **414 VM tests and 252 Chrome tests passed**, zero skipped or error events, and successful terminal `done` events for both platforms. Logs are retained at `/tmp/klukai-frontend-responsiveness-heti_9wu/{vm,chrome}.{stdout,stderr}`. The earlier focused Chrome run passed 164 tests, including streaming append, canonical `done.text`, interrupted replies, history reconciliation and voice playback. `flutter analyze --no-pub --fatal-infos` found no issues; `dart format --output=none --set-exit-if-changed lib test` checked 151 files with zero changes. `./scripts/deploy-web.sh --build-only` completed successfully, mirrored all 725 pinned fallback fonts with zero downloads, and passed its third-party origin audit. Build output is confined to the isolated worktree's `flutter_app/build/web`; `web-build/` and the running deployment were not changed.
