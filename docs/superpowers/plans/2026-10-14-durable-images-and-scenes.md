# Durable pictures and scenes implementation plan

> Use subagent-driven-development after session review fixes release shared files. Root integrates Git and coordinates lifecycle/Flutter overlaps.

**Spec:** docs/superpowers/specs/2026-10-14-durable-images-and-scenes.md
**Goal:** recover accepted account pictures without rerendering, and atomically preserve once-ever scenes.

## Global constraints

Existing PostgreSQL, private Unix worker and filesystem only. Preserve Agy/Grok/no-GPU/no-credit policies and owner reference boundaries. Migration210 reserved; bridge owns200. Do not modify historical migrations/records or reset existing oath. No new provider calls, accounts/dependencies, production data or services. Root will coordinate main.py lifecycle and any Flutter request/status seams after current tasks release them.

## Review focus

Crash ambiguity cannot become another generation attempt; lookup operation has no side effects; immutable payload/user scope; concurrent archive and file-cleanup safety; completed vs saved outcome; accepted jobs independent of WS; scene transaction consumes trigger only with complete history; existing event never replayed.

### Task 1: Durable image requests

**Files:** new app/account_image_jobs.py, agy_image_provider.py, scripts/agy_image_worker.py, memory_archive.py idempotent save seam, background.py image path, chat_handlers.py task ownership seam, routes.py image endpoints/models.py ImageGenRequest, migration210, main.py lifecycle coordinated, worker/core/real-PG focused tests and recovery runbook. Small client status handling only by prior coordination; otherwise expose authenticated API/gallery recovery.

- [ ] Read worker ledger/client/archive and write failure/restart/idempotency regressions first.
- [ ] Implement strict read-only cached-result recovery with exact immutable payload and scope validation; actual Unix HTTP tests assert zero renderer calls/rotation changes.
- [ ] Implement durable core request and bounded lifecycle ownership. Persist acceptance before native dispatch, recover only known claims, expose scoped status/conflicts.
- [ ] Implement stable archive identity with transaction/concurrency-safe retries and truthful failures; preserve all prior media/history.
- [ ] Integrate explicit chat/API generation without WS cancellation and preserve non-account paths.
- [ ] Run focused offline tests plus owned disposable PostgreSQL/file fixtures; lint/typecheck; report exact evidence/limitations in task-1-report.md under this plan's SDD directory. No commit/deploy.

### Task 2: Atomic once-ever oath scene

**Files:** new app/character_scenes.py if useful; background.py maybe_deliver_oath only; focused oath/history tests and docs. Coordinate background.py with task1 or execute after it; no new schema needed if existing firsts/history tables suffice.

- [ ] Add cancellation/concurrency/transaction-failure regressions reproducing partially consumed oath.
- [ ] Persist complete scene and firsts trigger in one transaction; live paced delivery uses persist=False.
- [ ] Respect existing triggers and empty/invalid configuration. Add real-PG proof for rollback and two concurrent callers using owned fixture.
- [ ] Run focused tests/lint/typecheck and write task-2-report.md; no live scene generation, production changes or commits.
