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
| Character research | Reconcile primary profile/quote material with existing dossier; attribute uncertainty and companion inventions; no unsupported claims promoted to canon | Audit in progress, bus e084b5b558e9 |
| Character behavior | Coherent identity, mature warmth, agency, pacing, addressing and context priorities across affection/mood states; no contradictory instructions | Prompt conflicts already found; implementation pending |
| Conversation state | Concurrent devices retain turns, broadcasts tolerate connection changes, and task lifetimes match session versus durable work | Offline lost-update and WS iteration reproductions; fixes pending |
| Database lifecycle | Migrations execute once with transactional bookkeeping, preserve legacy scores/gifts, serialize startup, and fail visibly on real errors | Startup replay defect confirmed; dedicated migration spec/plan |
| Durable scenes/jobs | Once-ever oath survives disconnect without being consumed prematurely; long jobs do not starve broker heartbeats or event delivery | Static findings pending implementation and behavioral verification |
| Presentation | Uncropped image composition, selectable/accessibly readable text, correct rich-content layout, voice/media controls preserved, coherent mobile/desktop layout | Crop and markdown-loss reproductions; implementation pending |
| Maintainability | Separate the responsibilities touched by these changes, remove redundant rendering/prompt policy paths, document current provider/runtime architecture | Pending |
| Quality gates | Missing snapshots/config failures cannot pass as green; distinguish prompt assembly checks from generated-answer evaluation; remove stale deployment/coverage claims | Golden harness and audit documentation gaps confirmed |
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

