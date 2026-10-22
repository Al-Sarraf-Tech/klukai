# CI acceleration implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline; the owner explicitly requested autonomous implementation and background execution.

**Goal:** Reduce queueing, repeated dependency setup and unrelated per-push work, retaining full exact-commit release verification.

**Architecture:** Keep fresh one-job VMs and the existing eight-thread host budget. Give quick checks priority, pack medium workloads, reserve full capacity for performance measurements, and persist only verified caches. Conservative affected-path planning selects routine gates; dispatch and release certification run the complete suite.

**Tech Stack:** Python, pytest, GitHub Actions, libvirt, Docker/BuildKit.

**Spec:** Owner-approved acceleration direction in the task handoff; release identity contracts in `tests/quality/test_ci_workflow_contract.py`.

## Global constraints

- Maximum eight CPU threads, 4096 MiB free-memory headroom, start below62C, pause68C/resume60C.
- No production application changes; no new spending or credential reads; preserve exact source/image/evidence validation.
- No Telegram tests or bot-only dependencies in active CI.
- Full release gates cannot accept skipped work or evidence from another revision.

## Review focus

- A queued large job must not block admissible smaller work.
- Restarting the controller must retain all running guest profiles and registrations.
- Corrupt caches must fail validation or rebuild; caches never certify a new source revision by themselves.
- Unknown/shared/dependency/workflow changes must select the full pipeline.
- Routine success must never become release certification.

### Task1: Runner scheduling and early checks

- [ ] Add failing scheduling tests for quick-first order, medium packing, blocked-profile backfill, exclusive performance admission and restart compatibility.
- [ ] Add medium/frontend profiles, prioritize quick work, reduce polling delay, and retain host guards.
- [ ] Update workflow labels and put expensive jobs behind fast checks.
- [ ] Verify controller and workflow tests; deploy the controller atomically with state preservation.

### Task2: Verified setup/build reuse and retired bot removal

- [ ] Test cache identity/corruption handling before implementation.
- [ ] Persist hash-verified pip downloads and BuildKit reusable materials while retaining fresh evidence per run.
- [ ] Reuse the scanned exact candidate for performance; retain independent no-cache rebuild and revision-bound baseline.
- [ ] Remove Telegram from active component target and CI lock without dropping shared runtime dependencies.
- [ ] Run component/release/workflow suites and full CI.

### Task3: Conservative routine selection and measured rollout

- [ ] Add failing path-planner tests for frontend/core/components/docs/shared/unknown and multi-component changes.
- [ ] Add explicit routine/full modes and distinguish their final status; strengthen release relay full-gate checks.
- [ ] Dispatch exact branch revision; inspect real results/timings; correct failures and repeat as needed.
- [ ] Integrate verified changes, commit conventionally, mirror locally and push authorized origin main.
- [ ] Record baseline and accelerated full/routine timings and remaining limitations in handoff evidence.
