# Chat Polish Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Klukai's conversation easier to compose and recover, with truthful status feedback.
**Architecture:** Retain the Flutter HUD, controller ownership, socket protocol and server boundaries. Add presentation state to the existing chat owner; make the composer respond to its controller and available width.
**Tech Stack:** Flutter/Dart; existing VM and Chrome widget tests.
**Spec:** `docs/superpowers/specs/2026-10-29-chat-polish-design.md`

## Global Constraints

No new dependencies, provider changes, automatic pictures, voice autoplay, or changes to stored memories. No production deployment is part of this checkout change.

## Review Focus

- Text scaling with voice controls must leave the field and actions usable.
- Programmatic controller updates and IME composition must not submit unfinished text.
- Reconnect refresh failure must preserve existing messages and recovery.
- Overlapping recent-history requests must not overwrite newer request state.
- Repeated scrolling after a pagination error must not hammer the server.

### Task 1: Responsive, accessible composer

Files: `flutter_app/lib/widgets/chat_composer_bar.dart`, `flutter_app/test/widgets/chat_composer_bar_test.dart`.
Interface: unchanged constructor/controller ownership.
- [x] Extend widget tests for useful field width at narrow/large-text sizes, blank/composing send guards, programmatic updates and independent picture action; observe failures.
- [x] Use a full-width field above a wrapping action row on constrained layouts; retain one row where it fits. Listen to the controller for send readiness, add an accessible field label, and guard submission.
- [x] Run `flutter test --no-pub test/widgets/chat_composer_bar_test.dart`; expect all passing.

### Task 2: Durable chat history recovery

Files: `flutter_app/lib/screens/chat_{screen,connection,view}.dart`, new `chat_history_state.dart` library part, `flutter_app/test/chat_screen_test.dart`.
Interface: existing history transport and merge remain; add request generation, loading/error state and explicit retry handlers to the single owner.
- [x] Add browser cases for initial loading/error/retry, reconnect preserving messages, stale completions and older-page retry throttling; observe failures.
- [x] Render a persistent, accessible history status strip; reserve empty starters for confirmed empty history. Show older-page retry at the top and suspend automatic retries until tapped. Guard recent-history completions by generation and auth state.
- [x] Run Chrome chat tests; expect all passing.

### Task 3: Truthful reply feedback and final verification

Files: `flutter_app/lib/screens/chat_composer.dart`, related timer comments, `flutter_app/test/chat_screen_test.dart`, `flutter_app/README.md`.
- [x] Replace obsolete cold-model copy with waiting-for-reply feedback; exercise timer cancellation on real status/reply/disconnect.
- [x] Correct frontend runtime notes to current cloud-only and on-demand picture policy.
- [x] Run formatter, analyzer, relevant frontend suites, cloud-only invariant and release build. Inspect a rendered fixture. Review the complete diff before final delivery.

## Execution notes

User's standing no-permission-prompts directive and authorized polish scope supersede workflow approval prompts. Work is isolated at `.worktrees/chat-polish-20261029` on `codex/chat-polish-20261029`; baseline ca76660. Existing untracked files in the primary checkout are untouched. Changes will be left reviewable in the user's checkout after verification; no publish, remote push or private conversation access.


## Verified completion — 2026-10-29

- Composer baseline: 10 passing cases. Five new cases failed before implementation (narrow/large-text geometry, draft readiness, IME button path); final composer suite has 15 passing cases. Independent review exposed the separate keyboard action path; `receiveAction(send)` reproduced one unintended send, then passed after preserving composition through editing completion.
- History: four new browser cases failed before implementation and passed afterward. They cover loading/error/confirmed-empty/retry, retained messages after reconnect failure, stale recent-history failure and pagination error suppression until explicit retry.
- Final browser run: `flutter test --no-pub --platform chrome test/chat_screen_test.dart test/screens/browser_journeys_test.dart` with the disposable Chrome wrapper: **122 passed**, exit 0. Includes the 200% text / open keyboard / microphone / four-line draft / history failure combination, a reachable Retry action, and preserved draft after recovery.
- Final VM run: composer, header, conversation presentation, message bubble and HUD contrast files: **49 passed**, exit 0.
- `python3 -m pytest docker/core/tests/test_cloud_only_invariant.py -q`: **19 passed**, exit 0. Core/provider code is unchanged.
- Flutter static analysis: no issues. Dart formatting and `git diff --check`: clean.
- `./scripts/deploy-web.sh --build-only`: exit 0; local CanvasKit, 725 verified fallback fonts from cache, third-party origin audit passed. Build remains in the isolated checkout; production web files were not published.
- Composer previews rendered at 390 px with normal and 200% text and inspected. Physical-device IME/screen-reader behavior remains outside this automated verification.

### Review and decisions

A fresh read-only reviewer found one important IME issue, no critical issues and no deferred minor issues. The keyboard-action regression test proved the issue before the fix and passes afterward. The reviewer also requested the combined layout case; it exposed a real overflow, now covered and fixed by placing the history notice within the transcript's available height. The complete browser/widget verification above ran after these fixes.

Ruling: include compact-header reflow and intrinsic tab height after the large-text chat test exposed fixed-size overflows. These remain in the existing HUD; the cost is a taller header/tab strip at large text sizes, which the viewport tests cover.

Ruling: cap the draft's visible lines according to available viewport height, passed from the chat owner because Scaffold adjusts descendant keyboard insets. Long drafts scroll inside the field and retain their full text. The cost is fewer simultaneously visible lines on short screens; the keyboard/microphone/retry test covers continued access and draft preservation.

Ruling: leave the verified source changes uncommitted in the user's original checkout, preserving its detached HEAD and pre-existing untracked files. No push, PR, deployment or live provider request was made. The isolated checkout is retained as build evidence.

## Deployment authorization

The owner subsequently requested “deploy it” on 2026-10-29. This authorizes committing and releasing the verified polish through the existing CI and atomic web deployment pipeline. The earlier no-deployment notes describe the completed implementation pass, before this request.
