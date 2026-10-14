# Durable job handoff implementation plan

> Use subagent-driven-development; root owns Git integration and full-suite runs.

**Goal:** reliable accepted Her POV jobs without blocking AMQP/event progress.
**Spec:** docs/superpowers/specs/2026-10-14-durable-job-handoff.md

## Global constraints

Existing Python, urllib, threading, pika, Redis and PostgreSQL only. No new dependencies/providers or live services. Migration200 is reserved for this task; append only, do not edit already-ledgered historical files. Root migration runner dcc9045 is available. Session agent owns background.py/chat.py/memory.py/ws_manager.py; do not edit those.

## Review focus

AMQP thread/channel ownership; reconnect delivery-tag reuse; repeated publication versus exactly-once cloud claim; queued-only SQL selection and bounded retries; truth of returned acceptance; no hanging shutdown or auth-failure job loss. Keep job and due-worker slots bounded.

### Task 1: Bridge execution and core queued-job reconciliation

**Files:** docker/events-bridge/bridge.py and new isolated bridge tests; docker/core/app/memory_her_pov.py queue/persistence functions; optional new app/job_handoff.py; main.py lifecycle only; events.py documentation; migration200; focused Her POV/handoff/main lifecycle tests; docs/architecture.md bridge section and recovery runbook.

- [ ] Add failing controlled-thread bridge tests for heartbeat/event/due progress and reconnect before completion; preserve evidence.
- [ ] Implement bounded workers, owner-loop result handling, connection generation checks and publish confirmations.
- [ ] Add failing core tests for durable queued acceptance and reconciliation crash/failure paths, including started-cloud exclusion.
- [ ] Implement throttled DB dispatch and startup/shutdown lifecycle. Return durable queued state despite absent Redis subscriber; do not create new generation attempts on retries.
- [ ] Run focused core and separately collected bridge tests; include a disposable existing PostgreSQL fixture for concurrent dispatch SQL if practical. Never collect bridge/core packages into one pytest namespace.
- [ ] Run scoped Ruff/mypy, update accurate behavior/recovery docs and report exact files, evidence and limitations.

Report: `.superpowers/sdd/2026-10-14-durable-job-handoff/task-1-report.md`. No commit/push/deploy. Do not run full core suite while other implementations are active.
