<!-- klukai PR template — Phase 3 S+ uplift -->

## Summary

<!-- One or two sentences. What does this PR do AND why. -->

## Tier impact

<!-- Pick all that apply. Add 1 sentence per dim explaining the delta. -->

- [ ] Code quality (linter / types / file sizes / dead code)
- [ ] Testing (coverage / new layers / fixtures)
- [ ] Security (secrets / SAST / image scan / deps)
- [ ] Reliability (healthchecks / restart / backup / DR)
- [ ] Observability (logs / metrics / traces / SLOs / runbooks)
- [ ] Performance (baseline / SLO / regression gate)
- [ ] Documentation (CHANGELOG / ADR / runbook / spec)
- [ ] Process (CI gates / release / review / rollback)
- [ ] None of the above (e.g., pure dependency bump)

## ADR

<!-- Did this PR introduce, modify, or supersede a decision? -->

- [ ] No (small / mechanical change)
- [ ] Yes → ADR-NNNN-...md included in this PR
- [ ] Yes → linked ADR in existing PR #...

## Tests

<!-- Required for any code change. Pure docs PRs can skip. -->

- [ ] Unit tests added / updated
- [ ] Integration tests pass locally / in CI
- [ ] Golden tests run (if personality / system_prompt touched)
- [ ] Manual smoke against klukai.appnest.cc/app/
- [ ] N/A (docs-only)

## Pre-merge checklist

- [ ] CI green (lint + test + typecheck + security + image-scan)
- [ ] Coverage didn't drop vs base branch
- [ ] CHANGELOG.md updated under `[Unreleased]`
- [ ] If touching deployable code: confirm restart plan
- [ ] If touching memory / chat / audit: confirm backward compat with
      existing user data (per `feedback_never_delete_chat.md`)

## Character integrity (klukai-specific)

<!-- Required for any PR touching personality/, affection.py, or
     system_prompt assembly. -->

- [ ] N/A (no character path touched)
- [ ] Klukai still addresses Commander as Commander (never Belka, Mechty,
      Andoris) — per ADR-0011
- [ ] Speech routing handles all affection levels 0-9 (per
      `feedback_speech_routing_bug.md`)
- [ ] No `(You ...)` narration introduced — only `(I ...)` per
      `app/personality/rules.py`
- [ ] No memory mutation paths that could delete chat history per
      `feedback_never_delete_chat.md`

## Deployment notes

<!-- Anything operator needs to know post-merge. -->

- [ ] No restart required (PR is config-only / docs-only)
- [ ] `companion-core` restart needed → `docker compose restart companion-core`
- [ ] `companion-voice` restart needed → on dominus-nobara
- [ ] Migration needed → `docker compose run companion-core alembic upgrade head`
- [ ] DB schema change → backup taken before merge

## Rollback plan

<!-- One line: how to revert. -->
