# Strict prompt snapshots

Goal: a prompt stability gate must fail when its evidence is missing, corrupt or cannot be assembled. It must never manufacture a passing baseline during ordinary verification.

Scope: `docker/core/tests/golden/test_system_prompt_snapshots.py` and a focused harness regression test. Character agent owns snapshot data and semantic rotation. This is prompt assembly verification, not generated-answer evaluation.

Contract: only exact `UPDATE_SNAPSHOTS=1` permits writing. Ordinary runs do not create directories or files. Missing, malformed, wrong-shape snapshots and assembly exceptions fail visibly. Compare all metadata as well as SHA; keep the cold-at-high-closeness invariant even while updating.

- [ ] Write regression cases for missing evidence, non-1 update values, malformed/invalid evidence, assembly errors, metadata drift and intentional creation.
- [ ] Observe failures with existing harness; implement the smallest strict change.
- [ ] Run harness tests independently of ongoing character rotation, then the 45 settled snapshots with the character task.
- [ ] Independently review with the quality-gate changes; document exact count and coverage limits.

Decision: root executes this narrow test-only task while product agents own separate files. No services, model calls, dependencies or deployment.
