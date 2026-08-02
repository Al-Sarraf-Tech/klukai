---
name: Feature request
about: Propose a new feature or enhancement to klukai
title: '[FEAT] '
labels: enhancement
---

## Summary

<!-- 1-2 sentences. What new capability is this asking for? -->

## Motivation

<!-- Why does klukai need this? Personal use case is fine — but be
     explicit so the future-you can recall the original justification. -->

## Proposed design

<!-- Sketch the approach. Skip if "spec-driven" via ADR (preferred). -->

## Phases

- [ ] Phase 1: MVP — minimum behavior change
- [ ] Phase 2: polish — error paths, edge cases, UI integration
- [ ] Phase 3: long-tail — observability, runbook, perf baseline

## Affects

- [ ] Chat (companion-core)
- [ ] Voice
- [ ] Image gen
- [ ] Memory archive
- [ ] Affection state
- [ ] Flutter PWA
- [ ] CI / CD
- [ ] Documentation only
- [ ] Other:

## Tier impact

<!-- Which tier dimensions does this advance / risk? -->

- [ ] Code quality
- [ ] Testing
- [ ] Security
- [ ] Reliability
- [ ] Observability
- [ ] Performance
- [ ] Documentation
- [ ] Process

## ADR required?

- [ ] Yes — this is a load-bearing decision (architecture / tech /
      cross-cutting policy). Author ADR first, then PR.
- [ ] No — small additive change

## Open questions

<!-- Things to resolve before starting work. -->

1.
2.

## Constraints

<!-- Anything that locks the design? Existing memories to respect? -->

- [ ] Must not break klukai's existing chat memory
      (`feedback_never_delete_chat.md`)
- [ ] Must not modify Commander persona
      (`feedback_commander_human.md`, ADR-0011)
- [ ] Must not require macOS / darwin (global CLAUDE.md)
- [ ] Must work on amarillo + dominus-nobara topology only (ADR-0002, ADR-0015)
