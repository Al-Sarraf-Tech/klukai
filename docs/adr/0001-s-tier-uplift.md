# ADR-0001: Adopt S+ tier uplift roadmap

- **Date:** 2026-05-16
- **Status:** Accepted
- **Authors:** jalsarraf, Claude
- **Spec:** `docs/superpowers/specs/2026-05-16-s-plus-uplift.md`

## Context

klukai entered Phase 1 at D-tier (perf unmeasured was the floor) per
`~/.claude/TIER_RUBRIC.md`. Phase 1 lifted to C+; Phase 2 to B. The
project is the user's primary AI companion serving 4 seed users at
klukai.appnest.cc. Continued ad-hoc improvements would land us at
A-tier within 1-2 months but never reliably at S+ without explicit
plan + calendar-bound gates.

## Decision

Adopt the 5-phase uplift roadmap in `docs/superpowers/specs/2026-05-16-s-plus-uplift.md`.
Each phase targets one tier level. Phase 5 includes 90-day calendar
soak (mutation kill sustained, quarterly drills, real incident
runbook usage).

Soonest realistic S+ certification: **2026-11-15** (6 months from
adoption). This isn't a slip — it's the honest cost of S+'s calendar
gates that can't be shortcut.

## Consequences

- Every PR is now measured against the rubric dimension floors.
- The `scripts/s-tier-audit.sh` single-command audit (Phase 5) becomes
  the canonical "are we S+?" check.
- File-size discipline (<500 LOC ideal, <300 for A+) drives the
  routes.py + proactive.py splits.
- CI grows from 5 jobs to ~12 (typecheck, security, image scan,
  mutation, contract, integration, perf, etc.) — runtime budget < 8min.
- 90-day calendar soak in Phase 5 means "S+ certification" cannot
  land before 2026-11-15 regardless of code velocity.

## Alternatives considered

- **Ad-hoc improvements**: faster initial gains but no convergence;
  would plateau at A-tier with drift in untouched dimensions.
- **Direct to S+ in one push**: rejected — many criteria are
  calendar-bound (quarterly drills) or external-infra-bound (SLSA L3).
- **A-tier as final goal**: accepted as a stopping point IF user
  declines Phase 5 effort; for now, S+ remains the target.

## Related

- `~/.claude/TIER_RUBRIC.md` — the rubric this plan walks against.
- `~/git/mcpservers/docs/superpowers/specs/2026-04-19-s-tier-uplift-design.md` — reference pattern.
- `CHANGELOG.md` — Phase 1 and Phase 2 entries.
