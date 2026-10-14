# Elite character and project refactor — acceptance map

Date: 2026-10-14. Active goal: 01a13c5d-c816-7022-9697-b095b3f44f22.
Base: 417b115. Working branch: codex/elite-character-refactor.

## Objective and authority

Deeply research and refactor Klukai's character, then improve the whole project to express her mature, composed, capable elite-Doll identity through behavior, presentation, architecture and reliability. The owner requests autonomous execution with no questions and as many agents as useful. Use only existing accounts, tools and infrastructure.

This map preserves the full objective. A passing unit suite or a single repaired subsystem is not completion. "Perfect" is treated as a demand for rigorous quality and a coherent product, not a factual guarantee that software can contain no undiscovered defects.

## Existing invariants

- Klukai remains the SST-05/HK416 character; the Commander remains human and male, with the approved fully anime, slightly lighter light-olive visual design.
- Preserve relationship history, memories, archived media, outfits, approved avatar assets, and affection progression. Never infer historical state repairs.
- Images use the authorized Agy/Grok account alternation/failover. No GPU image or paid-credit fallback.
- Existing Sonnet/creative/Brave authorizations remain scoped; voice remains Dominus XTTS with text-only availability on outages.
- No new accounts, subscriptions, paid services, dependencies acquired incidentally, or public/private-data uploads for research.
- Keep GPU gaming and idle guards. No Dominus stack mutation is required by the initial refactor.
- Canon sources, translations, interpretations and owner-specific companion fiction must be distinguishable.
- Use an isolated worktree, meaningful behavioral tests, independent reviews and current runtime evidence before completion.

## Workstreams and required evidence

| Workstream | Acceptance | Evidence/status |
|---|---|---|
| Character research | Reconcile primary profile/quote material with existing dossier; attribute uncertainty and companion inventions; no unsupported claims promoted to canon | Audit complete; character contract implementation in progress, bus 5180f89bb516 |
| Character behavior | Coherent identity, mature warmth, agency, pacing, addressing and context priorities across affection/mood states; no contradictory instructions | Focused 169 and strict golden 58 passed; final task report/review and generated-answer evaluation pending |
| Conversation state | Concurrent devices retain turns, broadcasts tolerate connection changes, and task lifetimes match session versus durable work | Reproductions complete; implementation bus 29a8e6517909, conversation-authority spec/plan |
| Database lifecycle | Migrations execute once with transactional bookkeeping, preserve legacy scores/gifts, serialize startup, and fail visibly on real errors | dcc9045, 58 focused including real PG passed; independent review 7b6ceef06577 no actionable findings. Pre-deploy backup/aggregate checks pending |
| Durable scenes/jobs | Once-ever oath survives disconnect without being consumed prematurely; long jobs do not starve broker heartbeats or event delivery | Static findings pending implementation and behavioral verification |
| Presentation | Uncropped image composition, selectable/accessibly readable text, correct rich-content layout, voice/media controls preserved, coherent mobile/desktop layout | Crop and markdown-loss reproductions; implementation pending |
| Maintainability | Separate the responsibilities touched by these changes, remove redundant rendering/prompt policy paths, document current provider/runtime architecture | Pending |
| Quality gates | Missing snapshots/config failures cannot pass as green; distinguish prompt assembly checks from generated-answer evaluation; remove stale deployment/coverage claims | Strict harness 9fc1dc8, 13 regressions passed after 9 failures/1 skip on old code. CI/docs and generated-answer checks pending |
| Whole-project verification | Core/ops/voice/bot/JS/Flutter checks and build; meaningful DB/Redis/job integration fixtures; visual inspection of representative narrow/wide layouts; independent review | Baselines below |
| Delivery | Review all findings, integrate authorized changes, verify deployed identity and essential flows while preserving state; retain source-linked handoff and limitations | Pending; no refactor deployment yet |

## Baseline and current evidence

- Core: 4,947 passed, 57 skipped, 1 deselected; 96.74% coverage. Log: private codex-tasks/klukai-elite-baseline-core.log.
- Flutter: 233 passed. Log: private codex-tasks/klukai-elite-baseline-flutter.log.
- Core skips are not evidence of integration coverage.
- Previous capability audit is complete and constrains choices; it did not migrate voice.
- Current git worktree is /home/jalsarraf/git/klukai/.worktrees/elite-refactor.
- The previous goal turn made progress by establishing account/capability evidence. This continuation uses new code/runtime evidence to choose the next actions.

## Execution decisions

- User's explicit autonomous/no-questions instruction supersedes optional skill approval stops. Record designs and reviews in files while continuing work.
- Independent file scopes may proceed in parallel under the user's agent instruction; shared-file changes and Git integration are serialized.
- Preserve discovered defects in the acceptance map until verified fixed. Do not call the overall goal complete merely because one plan finishes.

## Remaining acceptance inventory

Audit evidence is retained under `~/.local/state/codex-tasks/klukai-{character,architecture,interface}-refactor-audit-20261014.txt` (interface filename has the same pattern). These are actual source/runtime findings, not a promise that arbitrary whole-project rewrites are necessary.

- Character C1-C7: config wiring, coherent precedence, quirk fidelity, provenance, preserve relationship/voice design, generated-response evaluation B03-B14. First implementation running; live evaluation still pending.
- Architecture P1-A: once-only migrations implemented/reviewed; rollout checks pending.
- P1-B/P2-D: authoritative per-user turns, delayed state patches, WS generation cleanup — running.
- P1-C: nonblocking AMQP job execution, publisher confirmation and queued-job reconciliation — pending.
- P2-E: durable account-image IDs, immutable request payload, archive recovery without rerender, correct generated/archived outcomes — pending.
- P2-F: atomic complete once-ever oath persistence before paced live delivery, with no inferred replay of old events — pending.
- P2-G: component-separated CI, current runtime/coverage docs, truthful evaluation claims — pending.
- UI01-03: common selectable streaming/final renderer, correct rich content and full-frame accessible artwork — running.
- UI04-05: essential contrast and keyboard/semantic controls; message controls in current task, login/header/composer outstanding.
- UI06-07: stage wake/audio recovery/focus, follow-tail scroll, reduced motion including parallax — pending.
- UI08: speech-input permission/record/transcribe/error controller — pending.
- UI09: explicit recoverable gallery/profile/subscription/stage failure states — pending.
- UI10: shared visual roles/spacing, readable mobile layouts and source-owned login/build assets — pending.
- Final evidence: complete isolated core/ops/voice/bot/JS/Flutter checks, browser/render and 200% text/keyboard/motion acceptance, fresh whole-branch review, state-safe deployment and essential runtime flows — pending.

The process-local conversation coordinator is deliberately bounded to the deployed single uvicorn worker. A future multi-worker deployment requires distributed turn ownership; do not imply this refactor supplies it.

