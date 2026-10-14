# Interface states implementation plan

> Use subagent-driven-development, disjoint tasks in parallel; root serializes commits and final build/deploy.

**Goal:** finish interaction, accessibility and failure-state defects from UI04–10.
**Spec:** docs/superpowers/specs/2026-10-14-interface-states.md

## Global constraints

Existing dependencies/services only, no provider calls or production data. Keep approved character/couple references and avatar3d.js internals untouched. Do not edit generated web-build as source. Other agents own backend. Task1 starts only after conversation-presentation agent releases Flutter files; task2 is independent except root-owned build/CI coordination.

## Review focus

Permission and disposal races; typed draft preserved after transcription; no overlapping voice POST; honest data error vs empty; auth expiry; wake failure cannot block text; inert overlay excludes covered focus; follow-tail does not steal scroll; motion changes reset transforms; source-owned login actually reaches build output.

### Task 1: Flutter interaction and asynchronous states

**Files:** chat_screen.dart (composer/speech only, preserve new renderer), voice_button.dart, chat_header.dart, theme semantic roles, new speech controller/shared state widget, her_pov_screen.dart gallery state only, profile_screen.dart, subscription_screen.dart, focused widget/service/browser tests.

- [ ] Add failing tests for audited contrast/keyboard/state/error defects, preserving existing service and browser seams.
- [ ] Implement explicit speech lifecycle with safe late-result/disposal/cancel behavior and preserved typed draft.
- [ ] Make header/composer controls operable and readable at narrow widths/200% text.
- [ ] Implement recoverable gallery/profile/subscription states and retain last good data, with auth behavior intact.
- [ ] Run focused Flutter VM/Chrome tests plus analyze changed files; capture representative rendered evidence via the existing test runner.
- [ ] Report exact files, red/green results, render paths and limitations in `.superpowers/sdd/2026-10-14-interface-states/task-1-report.md`; no commit/deploy.

### Task 2: Stage lifecycle and source-owned login

**Files:** flutter_app/web/companion/app.js, companion.css, companion.html, optional small testable lifecycle module, web/index.html, new web/login.html copied from existing publicweb-build/login.html, scripts/deploy-web.sh source-copy handling only, focused Node/full-shell fixture tests.

- [ ] Add failing wake/audio/scroll/motion/error-focus tests, using existing test infrastructure and no real account endpoints.
- [ ] Implement wake and audio failure recovery, complete-response announcements, follow-tail/Latest and shared reduced-motion behavior.
- [ ] Implement provider-neutral portrait state with bounded read-only retry and preserved frames.
- [ ] Align semantic labels/contrast/focus/zoom, make login a source artifact and remove backup-only deployment dependency.
- [ ] Run companion/avatar Node suites, shellcheck changed deploy script and build-only when root coordinates Flutter build ownership. Capture current full-shell/login fixture screenshots with source hashes if supported; report limits honestly.
- [ ] Report exact files/results/render paths in `.superpowers/sdd/2026-10-14-interface-states/task-2-report.md`; no generated publish, commit or deploy.
