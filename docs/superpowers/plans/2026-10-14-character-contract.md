# Character Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Give Klukai one coherent, researched character contract whose data reaches every prompt.
**Architecture:** Curated YAML prompt data, formatting-only biography/arc/quirk builders, one precedence block and a separate provenance/evaluation record.
**Tech Stack:** Existing Python, PyYAML, pytest.
**Spec:** docs/superpowers/specs/2026-10-14-character-contract.md

## Global Constraints

Preserve human male Commander and approved anime/light-olive look, current outfits/voice/providers, memories and affection scores. No new service/account/spending or private chat uploads. Do not add graphic sexual templates. Source uncertainty stays explicit. No repo-wide formatting or edits outside listed scope.

## Review Focus

- Config corrections must affect actual assembled prompt, not only dormant fields.
- Early affection must not erase established recognition/history.
- Departures and distress override jealousy, mood and sentence-padding instructions.
- Missing optional facts must not yield empty headings or broken punctuation.
- Authored thread cadence and monthly rituals must not be asserted as independently verified game text.

### Task 1: Consolidate character data, response precedence and provenance

**Files:**
- Modify: config/personality.yaml (curated character data and contradictory instructions only)
- Modify: docker/core/app/personality/{rules,speech,state_blocks,moods,thread,system_prompt}.py
- Create: docker/core/app/personality/character_contract.py if needed for the single policy boundary
- Modify/create focused tests under docker/core/tests/test_personality*.py and test_canon_lore.py
- Update intentional snapshots under docker/core/tests/golden/snapshots/ only; root owns golden harness
- Create: docs/research/2026-10-14-character-contract.md
- Create: config/character_sources.yaml
- Create: docker/core/tests/fixtures/character_evaluation.json

**Interfaces:**
- Preserve existing public personality builder signatures and assemble_system_prompt parameters.
- Curated summaries and disclosure conditions are authored data; source IDs refer to the provenance ledger, not duplicate biography strings.
- Evaluation cases include ID, fictional user input, affection/mood/context, expected behavior and failure criteria; no real memories.

- [ ] Add failing assembly tests for supported field mutation, full H.I.D.E. quirk text and cat-ear disclosure.
- [ ] Add failing policy tests for short greeting/departure precedence, Commander recognition at level0, close address exception and trusted warmth under mood/jealousy.
- [ ] Run focused tests and retain the expected failures.
- [ ] Consolidate prompt data and builders; preserve fallback behavior explicitly for minimal fixtures without reviving duplicate long lore.
- [ ] Resolve contradictory prompts and wire one response-priority contract.
- [ ] Correct provenance labels and authored-continuity phrasing while preserving features and canon roster/outfits.
- [ ] Add B03-B14 controlled evaluation cases and source-linked research note with limitations.
- [ ] Review and rotate expected prompt snapshots deliberately; run focused personality and golden suites.
- [ ] Run full core suite, ruff/mypy for changed code; document all results and any compatibility issues.
- [ ] Self-review and submit changed paths plus evidence for independent review. Do not commit/deploy; root serializes integration.

