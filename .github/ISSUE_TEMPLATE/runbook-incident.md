---
name: Runbook incident
about: File a post-incident report when a runbook was followed (or should have been)
title: '[INCIDENT] '
labels: incident, runbook
---

## Incident

**Date / time observed**:
**Severity**: P1 / P2 / P3 / P4
**Detected via**: alert / user report / monitoring / manual check

## What broke

<!-- 1-2 sentences. Match a runbook symptom if applicable. -->

## Runbook used

- Runbook: `docs/runbooks/____.md`
- [ ] Runbook steps led to resolution
- [ ] Runbook was partially helpful (note gaps below)
- [ ] No runbook existed for this failure → new runbook needed (link PR)

## Timeline

- HH:MM — issue began (best estimate)
- HH:MM — detected
- HH:MM — immediate mitigation applied
- HH:MM — root cause identified
- HH:MM — fully resolved

## Root cause

<!-- What actually went wrong, not just the symptom. -->

## What worked

<!-- What did the runbook get right? -->

## What didn't work

<!-- Steps that wasted time, missing information, etc. -->

## Action items

- [ ] Update runbook (link PR):
- [ ] Add monitoring / alert:
- [ ] Add automated test that would catch this:
- [ ] File new ADR if architecture decision needed:
- [ ] Other:

## Related

- [ ] ADR: docs/adr/NNNN-...md
- [ ] Previous similar incidents: #...
- [ ] CHANGELOG.md entry under `[Unreleased]`

<!-- Per global CLAUDE.md feedback_never_delete_chat.md: if this
     incident involved chat memory or Qdrant vectors, the recovery
     MUST come from backups, never from rebuild. Confirm. -->
