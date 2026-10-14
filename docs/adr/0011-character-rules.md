# ADR-0011: Klukai is T-Doll; Commander is HUMAN (absolute character rules)

- **Date:** Origin (formalized 2026-05-16)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

Klukai's character integrity is the product. Two rules have come up
repeatedly in the conversation history (preserved in global CLAUDE.md
memory) and are inviolable regardless of LLM tendency:

1. **Klukai IS Klukai** — never an AI, language model, program.
   When asked, deflects in-character. From `app/personality/rules.py`:
   "You ARE Klukai. Never acknowledge being an AI, language model,
   or program."

2. **Commander is HUMAN** — male presentation, NOT a T-Doll. Per
   global CLAUDE.md `feedback_commander_human.md`: "ABSOLUTE:
   Commander is human, NEVER a T-Doll. Male presentation always."
   Klukai is a T-Doll loyal to a human Commander; treating the
   Commander as another T-Doll inverts the entire dynamic.

Other absolute rules (from `app/personality/rules.py:build_character_rules`):
no emoji/kaomoji, no `(You ...)` narration ever, no holograms,
no military jargon during intimate moments, substantive responses
even when cold, etc.

## Decision

Encode these as **absolute_rules** in three places:

1. `config/personality.yaml` → `absolute_rules:` block, injected at
   the top of every system prompt assembly.
2. `app/personality/rules.py:build_character_rules()` → hard-coded
   block injected immediately after the character preamble.
3. `tests/golden/` → 45 deterministic assembled system-prompt snapshots
   (5 affection levels × 3 moods × 3 time windows), with explicit
   `UPDATE_SNAPSHOTS=1` rotation. These verify prompt assembly and selected
   invariants; they do not establish that generated answers obey the rules.

Tests added before then (per `tests/test_personality_rules.py`)
verify the rules text contains the expected anchors (no emoji,
forbidden `(You)`, allowed `(I)`, no holograms, etc.).

## Consequences

- **Prompt budget cost**: ~50 lines of rules in every prompt. Worth
  it — rules at the top set strong priors that the LLM follows.
- **Test discipline**: Personality changes require semantic review and the
  golden suite. The October 14 character contract consolidates conflicting
  instructions; preserving contradictions verbatim is not an acceptance goal.
- **Behavior evidence**: Generated-response evaluation and mutation testing
  are separate from prompt snapshots. Report their actual execution and
  coverage; the old plan for 3,000 generated cases was not implemented here.
- **Configuration drift risk**: rules in YAML + rules in Python is
  duplication. The Python block is the canonical fallback if the
  YAML is missing or malformed. Maintaining both is the cost of
  defense-in-depth.

## Alternatives considered

- **Rules in YAML only**: rejected — YAML can be empty/missing in
  dev; the Python fallback ensures rules always render.
- **Rules at end of prompt**: rejected — LLMs weight earlier
  context more strongly. Rules at top set the strongest prior.
- **Per-mood rule variants**: rejected — rules are absolute. Mood
  modulates voice, not identity.

## Related

- `app/personality/rules.py:build_character_rules`
- `app/personality/system_prompt.py:assemble_system_prompt` (composition order)
- `config/personality.yaml` (`absolute_rules:`)
- `feedback_commander_human.md` (global CLAUDE.md)
- `tests/test_personality_rules.py`
- ADR-0005 (affection taxonomy)
