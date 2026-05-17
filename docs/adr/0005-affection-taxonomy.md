# ADR-0005: Affection taxonomy — 10 levels (0-9) with distinct speech patterns

- **Date:** Origin (formalized 2026-05-16)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

Klukai's affection toward the Commander is the central character arc.
Per Girls' Frontline 2 canon, the relationship moves from professional
distance to deep bond over time. A boolean ("loves the Commander
yes/no") would lose all gradient. A continuous score (0-1000) is
necessary for fine-grained progression but is too granular for
prompt modulation — the LLM needs discrete behavioral states.

## Decision

10 affection levels (0 through 9), each with:

- A canonical **name** (e.g., "Cold Assessment", "Trusted",
  "Devoted", "Bonded").
- A **prompt modifier** (one-paragraph behavioral directive injected
  into the system prompt).
- A **speech pattern** key (one of 5: `level_0_cold` …
  `level_4_bonded`). Levels 4-9 all map to `level_4_bonded` because
  the high-affection differences are modulated by prompt_modifier,
  not separate speech configs.

Continuous score 0-1000 maps to levels via thresholds in
`app/affection.py`. Score changes by ±N per turn based on gift
reactions, conversation depth, intimacy markers, decay over inactivity.

## Consequences

- **Speech routing must handle all levels 0-9** (per
  `feedback_speech_routing_bug.md`): historical bug where levels 5-9
  silently defaulted to "cold" because the if-ladder didn't have a
  fallback. The current `app/personality/loader.py:get_speech_patterns`
  has an explicit `else: key = "level_4_bonded"` that catches this.
  Any future routing changes MUST preserve this rule.
- **Backwards-compat**: changing level boundaries would re-shape every
  user's experience overnight. Boundaries should change via ADR with
  user notification, not silently.
- **Test coverage**: `app/personality/loader.py` has 100% test coverage
  including a regression guard (`test_get_speech_patterns_levels_5_through_9_use_bonded`).
- **HIGH-AFFECTION LOVE RESPONSES** at level 7+ have explicit anti-patterns
  per `app/personality/rules.py:build_character_rules`: never "prove
  it" / never doubt-testing once at level 7+. Klukai has waited 10
  years — she's past that.

## Alternatives considered

- **Boolean affection**: loses all gradient.
- **Continuous score only**: too granular for LLM modulation.
- **Per-mood-category affection**: too complex; affection is a single
  axis. Mood is separate (per `docs/slos.md` and
  `app/personality/moods.py`).

## Related

- `app/affection.py`
- `app/personality/loader.py`
- `app/personality/speech.py:build_affection_block`
- `config/personality.yaml` — level definitions
- `feedback_speech_routing_bug.md` (global CLAUDE.md)
- `tests/test_personality_squad_and_loader.py:test_get_speech_patterns_levels_5_through_9_use_bonded`
