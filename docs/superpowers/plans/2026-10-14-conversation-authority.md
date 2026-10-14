# Conversation authority implementation plan

> Use subagent-driven-development; root serializes Git and reviews independently.

**Goal:** eliminate stale live context writes and give socket cleanup explicit ownership.
**Spec:** docs/superpowers/specs/2026-10-14-conversation-authority.md
**Evidence:** private architecture audit P1-B/P2-D; deployed single uvicorn worker.

## Global constraints

Preserve history, approved providers, no-spending limits and per-user scope. Use existing Python/Redis/testing packages only. No production database/service access. Other agents own personality and Flutter. Later durable image/oath task will also touch background.py, so complete/release these files before that task begins.

## Review focus

Current state must be read under ownership; a lock around stale objects is insufficient. Conditional state must also gate persistent mood and mission side effects. Aborted turns cannot allow older delayed work back in. Cancellation/reconnect cannot cancel the new connection generation. No global lock blocking independent users.

### Task 1: Session authority and WebSocket cleanup

**Files:** app/memory.py, models.py (SessionState only), chat.py, chat_handlers.py (admission/background snapshot seams only), background.py (extraction state/compaction only), ws_manager.py, optional new app/session_coordinator.py; focused session/WS/chat/background tests; docs/architecture.md session ownership section only.

**Interfaces:** preserve get_session/save_session/add_turn and public chat handlers where practical; add explicit session ownership and conditional patch APIs. Preserve WS event types.

- [ ] Read actual callers/tests and write failing reproductions for two open devices, delayed patch/compaction, Redis failures and connection churn.
- [ ] Implement coordinator, revision and latest-state admission. Update connect and text/voice flows.
- [ ] Make state saves fail visibly when Redis persistence fails; keep unrelated recall fail-soft behavior.
- [ ] Deep-copy background inputs and apply mood/mission and compaction through conditional latest-state operations.
- [ ] Snapshot WS membership and centralize generation-safe, awaited task cleanup and exception retrieval.
- [ ] Run focused tests with controlled scheduling/barriers; no arbitrary sleeps or provider calls. Use a disposable cached Redis fixture if it materially verifies the real storage boundary, never production Redis.
- [ ] Run Ruff/mypy on changed product code, report exact changes/tests/limitations; no commit or deployment.

Report: `.superpowers/sdd/2026-10-14-conversation-authority/task-1-report.md`. Root will run the integrated core suite after source settles.
