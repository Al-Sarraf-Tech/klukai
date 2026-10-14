# Once-only Migrations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make database startup repeatable without changing existing relationship state or hiding migration failures.
**Architecture:** Dedicated transactional, advisory-locked migration runner with checksum ledger; guarded legacy conversion and seed.
**Tech Stack:** Existing Python, psycopg/psycopg_pool, PostgreSQL, pytest.
**Spec:** docs/superpowers/specs/2026-10-14-once-only-migrations.md

## Global Constraints

No new services/accounts/spending. Use an isolated disposable database for integration checks. Preserve all current memory, score and gift rows; do not repair historical state by inference. Never use production DATABASE_URL for test setup. No source edits outside this task's files without coordination.

## Review Focus

- Existing current schema with a low affection score must not be scaled again.
- Concurrent startup must serialize the ledger and migrations.
- Failed SQL and ledger writes must roll back together.
- Recorded migrations changing or disappearing must stop startup.
- Existing duplicate gifts/history must remain intact.

### Task 1: Migration runner and safe legacy adoption

**Files:**
- Create: docker/core/app/migrations.py
- Modify: docker/core/app/main.py (run_migration only and necessary imports)
- Modify: docker/core/migrations/030_affection_scale_and_read_at.sql
- Modify: docker/core/migrations/080_new_features.sql
- Create: docker/core/tests/test_migrations.py
- Create: docker/core/tests/test_migrations_postgres.py
- Modify: docker/core/tests/test_main_coverage.py (migration tests only)
- Create: docs/runbooks/database-migrations.md

**Interfaces:**
- Consumes: pool.connection() returning psycopg async connection, migration SQL files.
- Produces: async apply_migrations(pool, migration_dir: Path) -> list[str]; main.run_migration() remains stable.

- [ ] Write failing focused tests for lexical application, repeated startup, missing directory, checksum mismatch, failed SQL/ledger atomicity, and concurrent runners.
- [ ] Add real PostgreSQL fixture scenarios using only KLUKAI_MIGRATION_TEST_DATABASE_URL: fresh schema, pre-030 score5 to50 once, current schema score5 unchanged, pre-existing duplicate gifts retained.
- [ ] Run the new tests against current code and retain expected failing evidence.
- [ ] Implement the runner, 30-second lock wait, transaction-scoped lock, companion_schema_migrations ledger and SHA-256 validation.
- [ ] Guard 030 with its read_at schema marker and 080 seed with a user/item existence query; review every other SQL script for replay safety.
- [ ] Replace main's broad exception-swallowing loop with delegation; replace tests that assert failure swallowing with failure-propagation behavior.
- [ ] Run focused tests and real PostgreSQL tests; document disposable image/container identity and cleanup.
- [ ] Run core suite plus ruff/mypy for changed code; report failures honestly.
- [ ] Write the migration runbook including legacy adoption, failed-start diagnostics, backup and state-preserving rollback constraints.
- [ ] Self-review; submit exact changed-file list and test evidence for independent review before integration.

