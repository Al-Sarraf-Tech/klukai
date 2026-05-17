"""Performance test layer — perf-gate target.

S+ Phase 4 (per docs/superpowers/specs/2026-05-16-s-plus-uplift.md §5.5).
Runs `tools/load-test/probe.py` against a live klukai stack and asserts
per-endpoint SLO targets defined in `docs/slos.md`.

These tests are marked `perf` and excluded from default pytest runs —
run with `pytest -m perf` against a live stack.
"""
