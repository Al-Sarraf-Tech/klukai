# Conversation Presentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Present every answer and full artwork accessibly without losing the established character styling.
**Architecture:** Unified message container, native selectable rich-content layout, safe checked-in-parser adapter, shared artwork and footer.
**Tech Stack:** Existing Flutter/Dart, checked-in marked JavaScript, Node tests and Flutter Chrome tests.
**Spec:** docs/superpowers/specs/2026-10-14-conversation-presentation.md

## Global Constraints

No new dependency download/account/service/spending. Preserve existing browser/TTS seams and approved avatar/art bytes. No live backend requests in tests. Source edits only; do not edit generated web-build or deploy. Root serializes commits.

## Review Focus

- Finalization must not remove words, replay or selection when pretext is available.
- Unknown or malformed Markdown must fall back visibly, never disappear.
- Links/HTML/images must not create executable or automatic remote content.
- Landscape/portrait art must retain all edges and fit narrow screens.
- Enlarged text, keyboard and parse failures must preserve usable controls.

### Task 1: Unified content and artwork presentation

**Files:**
- Modify: flutter_app/lib/widgets/message_bubble.dart
- Modify: flutter_app/lib/screens/chat_screen.dart (renderer selection/obsolete width use only)
- Replace/remove: flutter_app/lib/widgets/canvas_message_bubble.dart as needed after checking references
- Create focused message_content/message_footer/artwork widgets and markdown adapter/model under flutter_app/lib/widgets, services or models
- Modify: flutter_app/lib/screens/her_pov_screen.dart (completed artwork presentation only)
- Modify: flutter_app/web/js/pretext_bridge.js only for safe structured parser access or retiring unused answer canvas functions
- Modify/create focused Flutter and tools/companion/test/pretext_bridge.test.mjs tests
- Update: flutter_app/lib/theme/gfl2_colors.dart only if a semantic role is missing

**Interfaces:**
- Preserve MessageBubble(message:, browser:, client:) public behavior.
- BrowserPlatform.openUrl remains the navigation boundary; permit only http/https source links at the message-content boundary.
- Text layout/content is independent of TTS request ownership; one footer triggers existing chunked playback.
- Keep source text available whenever the structured renderer cannot preserve a token.

- [ ] Add failing tests reproducing finalized quote/table loss, list numbering, missing replay and pretext-ready route inconsistency.
- [ ] Add behavior tests for parser errors, unsupported/malformed content, unsafe links/HTML and Markdown image no-fetch.
- [ ] Add fixture-image geometry/full-view tests with visible edge markers, invalid bytes and keyboard action labels.
- [ ] Run new tests against current code; retain expected failures.
- [ ] Implement unified selectable accessible content with native reflow and shared footer; remove the canvas-only finalization route.
- [ ] Implement contained artwork and full-view/download actions; reuse for completed Her POV without altering gallery semantics.
- [ ] Check narrow/wide layouts, 200% text, resize and actual semantic/control behavior with Flutter's existing browser test runner.
- [ ] Run Node companion tests, Flutter tests including browser-only suites, flutter analyze and release build without new downloads.
- [ ] Retain fixture screenshots if existing test tooling supports them, with viewport/source identity and no live backend calls.
- [ ] Self-review; report exact changed paths, red/green evidence, visual limitations and remaining UI audit items. Do not commit/deploy.

