"""Golden test layer — character regression guards.

S+ Phase 4 (per docs/superpowers/specs/2026-05-16-s-plus-uplift.md §5.7).
Snapshots system-prompt output across (affection_level, mood, time_of_day)
tuples. Drift = test fail; rotation requires explicit `--update-snapshots`.

Klukai's character is the product. A regression in speech pattern, mood
handling, or affection-level transition is functionally an outage.
"""
