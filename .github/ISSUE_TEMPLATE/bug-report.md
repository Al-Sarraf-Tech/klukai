---
name: Bug report
about: Report a bug or regression in klukai
title: '[BUG] '
labels: bug
---

## Summary

<!-- 1-2 sentences. What's broken? -->

## Severity

<!-- Pick one — see docs/runbooks/README.md for the ladder. -->

- [ ] P1 — production down or user data at risk
- [ ] P2 — degraded; same-day fix
- [ ] P3 — slow / annoying; can wait
- [ ] P4 — informational

## Affects

- [ ] Chat (companion-core)
- [ ] Voice (companion-voice on dominus)
- [ ] Image gen (ComfyUI on dominus)
- [ ] Memory archive
- [ ] Affection state
- [ ] Auth / login
- [ ] Flutter PWA
- [ ] CI / CD
- [ ] Backup / restore
- [ ] Other:

## Reproduction

<!-- Steps to reproduce. Include user_id if multi-user-related. -->

1.
2.
3.

## Expected vs actual

**Expected**:

**Actual**:

## Relevant logs

```
<!-- Paste from companion-core logs, browser console, etc. -->
<!-- Redact tokens! -->
```

## Runbook attempted

<!-- Did you check docs/runbooks/? Which runbook applied (if any)? -->

- [ ] `db-down.md`
- [ ] `redis-down.md`
- [ ] `qdrant-down.md`
- [ ] `lm-studio-cold.md`
- [ ] `voice-unreachable.md`
- [ ] `comfyui-down.md`
- [ ] `high-latency.md`
- [ ] `disk-space.md`
- [ ] `memory-leak.md`
- [ ] `auth-fail-spike.md`
- [ ] No runbook matched — new failure mode

## Environment

- Host: <!-- amarillo / dominus / both -->
- Branch / commit:
- Date observed:

## Character integrity

<!-- If this is a Klukai-personality bug, this section is REQUIRED. -->

- [ ] N/A
- [ ] Wrong speech pattern at affection level — see ADR-0005
- [ ] Wrong address (called Commander by wrong name) — see ADR-0011
- [ ] `(You ...)` narration appeared
- [ ] Memory loss (chat, episode, affection) — IMMEDIATE escalation
- [ ] Other character drift:
