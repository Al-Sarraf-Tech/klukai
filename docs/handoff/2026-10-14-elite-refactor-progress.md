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
| Character behavior | Coherent identity, mature warmth, agency, pacing, addressing and context priorities across affection/mood states; no contradictory instructions | 330b45e; extended441 passed. Review157446d3cc31 caught duplicated catchphrase directive; root fixed with four RED→GREEN mutation cases, refreshed45 snapshots and234 focused passes. character-eval.log (2026-10-15, Dominus `cognitivecomputations_dolphin-mistral-24b-venice-edition`, revision `git:e592d06e667f+personality:3c928809a33b+contract:7985d83a1fb9+memory:e647a07284a1+squad:9f1b65a0db8f+departure`): 31 fictional cases, reviewer_pass=31, reviewer_fail=0. B05 is `Sleep, Commander. ...I'll be here.` Record: `docker/core/tests/fixtures/character_evaluation_record.json` (`pass_count` 31, `fail_count` 0), separate from golden snapshots. Prompt assembly, including the fail-closed missing-snapshot and bad-config cases, is inside core-suite.log. |
| Conversation state | Concurrent devices retain turns, broadcasts tolerate connection changes, and task lifetimes match session versus durable work | Reproductions complete; implementation bus 29a8e6517909, conversation-authority spec/plan |
| Database lifecycle | Migrations execute once with transactional bookkeeping, preserve legacy scores/gifts, serialize startup, and fail visibly on real errors | dcc9045, 58 focused including real PG passed; independent review 7b6ceef06577 no actionable findings. Deploy adopted 22 migrations once; a second startup added none. Backup `/home/jalsarraf/backups/klukai/pre-99508d2.dump` restored with affection 2 rows totaling 2000, gifts 634, and messages 1121. |
| Durable scenes/jobs | Once-ever oath survives disconnect without being consumed prematurely; long jobs do not starve broker heartbeats or event delivery | durable-postgres.log: 11 passed, pytest_exit=0, including a queued Her POV job with no subscriber and atomic oath rollback, concurrency, and an existing trigger. image-recovery.log: 5 passed, pytest_exit=0, including `test_chat_picture_survives_disconnect_and_repeats_without_another_render`. core-suite.log at 24d7ef7: 5060 passed, 57 skipped, 1 deselected, 8 warnings, core_exit=0. |
| Presentation | Uncropped image composition, selectable/accessibly readable text, correct rich-content layout, voice/media controls preserved, coherent mobile/desktop layout | Interface suite passed: Flutter VM 44, Chrome chat/presentation/bubble 57, companion stage and login 9. Full Flutter VM 249 and Chrome 325 also passed. Headless Chrome rendered the companion shell at 1280x800 (luma 6–234) and 390x844 (luma 5–234) with a long fixture reply and a non-square artwork frame. |
| Maintainability | Separate the responsibilities touched by these changes, remove redundant rendering/prompt policy paths, document current provider/runtime architecture | Pending |
| Quality gates | Missing snapshots/config failures cannot pass as green; distinguish prompt assembly checks from generated-answer evaluation; remove stale deployment/coverage claims | Strict harness 9fc1dc8. On 2026-10-15 core-suite.log at 24d7ef7 includes the fail-closed missing-snapshot and bad-config cases: 5060 passed, 57 skipped, core_exit=0. Generated-answer judgments are in character-eval.log and the character evaluation record, not in the golden snapshots. |
| Whole-project verification | Core/ops/voice/bot/JS/Flutter checks and build; meaningful DB/Redis/job integration fixtures; visual inspection of representative narrow/wide layouts; independent review | Baselines below |
| Delivery | Review all findings, integrate authorized changes, verify deployed identity and essential flows while preserving state; retain source-linked handoff and limitations | Deployed 2026-10-15 from 99508d2. companion-core and events-bridge were recreated; health returned ok. The first startup adopted 22 migrations. A second startup added none. Affection stayed 2 rows totaling 2000, gifts 634, messages 1121. Backup: `/home/jalsarraf/backups/klukai/pre-99508d2.dump`, restored into disposable Postgres 17 with the same counts. |

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

- Character C1-C7: config wiring, coherent precedence, quirk fidelity, provenance, preserve relationship/voice design, generated-response evaluation B03-B14. character-eval.log rejudged 2026-10-15 on Dominus: reviewer_pass=31, reviewer_fail=0. B05 is `Sleep, Commander. ...I'll be here.`
- Architecture P1-A: once-only migrations implemented/reviewed. Production startup applied 22 files once; the next startup applied none. Affection, gifts, and message counts were unchanged.
- P1-B/P2-D: authoritative per-user turns, delayed state patches, WS generation cleanup — running.
- P1-C: nonblocking AMQP job execution, publisher confirmation and queued-job reconciliation — implementation bus9db2c47d8579 running, migration200 reserved.
- P2-E: durable account-image IDs, immutable request payload, archive recovery without rerender, correct generated/archived outcomes — image-recovery.log: 5 passed, pytest_exit=0. `test_chat_picture_survives_disconnect_and_repeats_without_another_render` keeps one renderer call and the same saved request id after the last device disconnects.
- P2-F: atomic complete once-ever oath persistence before paced live delivery, with no inferred replay of old events — durable-postgres.log: 11 passed, pytest_exit=0. Existing triggers are not replayed. Historical rows were not repaired.
- P2-G: component-separated CI stays on `runs-on: [self-hosted, dominus]`. The workflow change is in the tree and has not been run by that runner yet. character-eval.log records reviewer_pass=31 and reviewer_fail=0.
- UI01-03: common selectable streaming/final renderer, correct rich content and full-frame accessible artwork — Chrome presentation and message-bubble tests passed, including artwork that is not cropped square.
- UI04-05: essential contrast and keyboard/semantic controls — header disconnect, composer, and voice passed at 44px with keyboard activation. Login is owned by `flutter_app/web/login.html`.
- UI06-07: stage wake/audio recovery/focus, follow-tail scroll, reduced motion including parallax — companion stage tests passed 9, including follow-tail, reduced motion, and wake without audio.
- UI08: speech-input permission/record/transcribe/error controller — a transcript keeps the typed draft, text still sends while voice is down, and a cancelled transcript does not replace the draft.
- UI09: explicit recoverable gallery/profile/subscription/stage failure states — empty is distinct from error, and a failed refresh keeps the last good gallery, profile, and subscription data. A failed subscription does not look like verified elite.
- UI10: shared visual roles/spacing, readable mobile layouts and source-owned login/build assets — header has no overflow from 320px through 1280px. Login source matches the checked-in public page and the deploy script copies that source.
- Final evidence from suites.log: core-suite.log at 24d7ef7 is 5060 passed / 57 skipped / core_exit=0. Flutter VM 249 and Chrome 325 passed. Companion JavaScript tests (`tools/companion/test`) passed 113 with 0 failures. `make test-js` is not green: it exited 2 because two personal-use dev-GLB checks failed, neck and stomach missing at 0 degrees, when `assets/build/dev/klukai.glb` is present. The earlier worktree run that skipped those assets is not this result. Events-bridge 20 passed / 3 skipped because pika is not installed. Flutter, events-bridge, and interface sources are unchanged since d27d4b1. Browser screenshots of a long reply with artwork are 1280x800 (luma 6–234) and 390x844 (luma 5–234). The live `web-build/index.html` references `flutter_bootstrap.14a8b20a.js`, which returns HTTP 200. `web-build/js/pretext_bridge.js` matches `flutter_app/web/js/pretext_bridge.js`. Production cutover of companion-core and events-bridge completed 2026-10-15; health returned ok. A fresh whole-branch review was not part of this cutover.

The process-local conversation coordinator is deliberately bounded to the deployed single uvicorn worker. A future multi-worker deployment requires distributed turn ownership; do not imply this refactor supplies it.

## Fresh component checks

After the first character/migration commits, unaffected components passed separately: ops197 plus4 subtests; voice33; shellcheck clean. Bot initially could not collect under system Python (missing telegram); using the already-installed `tgbot/.venv/bin/python` passed36. No packages installed. Logs: private codex-tasks/klukai-elite-{ops,voice,shell,tgbot-venv}.log. These results cover those unchanged components at this point, not the pending core/UI changes.

Character review disposition: the supported catchphrase field really did conflict with a literal absolute rule, so the rule now governs only use of the configured value. Four new assembly regressions failed first at levels0/9 with changed/omitted catchphrase, then passed. One legacy unit expectation was changed to the new single-source contract. All45 golden changes are exactly the expected one-character length increase and changed SHA for this single policy-line replacement; other metadata is unchanged. Focused personality/canon/golden checks remained green. core-suite.log at 24d7ef7 is 5060 passed, 57 skipped, and core_exit=0 on a disposable database. character-eval.log records 31 pass and 0 fail, separate from the prompt snapshots.

