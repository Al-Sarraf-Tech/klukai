# Once-only database migrations

## Purpose

Repeated application startup currently replays every SQL file and suppresses every SQL error. Migration 030 multiplies scores and migration 080 inserts duplicate gifts. Startup must protect relationship state and refuse to run against an unverified schema.

## Design

Extract a dedicated migration runner from main.py. One PostgreSQL connection and transaction acquire a transaction-scoped advisory lock, create a migration ledger, validate recorded checksums, execute pending files in lexical order, and record each successful file. An exception rolls back the transaction and propagates through startup. Do not log SQL bodies or credentials.

Use companion_schema_migrations(name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()). SHA-256 is over the exact UTF-8 file bytes. Validate the applied/present mapping before executing pending work; a changed recorded file or missing recorded file is a startup error. The lock must have a finite wait (30 seconds) so an unhealthy concurrent deploy cannot hang indefinitely.

For installations predating the ledger, safely apply the historical scripts once. Make the two non-idempotent historical operations idempotent before adopting tracking:
- 030 scales scores only when companion_messages.read_at is absent, and creates the column in the same transaction. Presence of that historical schema marker means the conversion has already run.
- 080 inserts the canonical Klukadile seed only if that user/item does not already exist. Existing duplicate gift rows are preserved.
All other historical scripts are reviewed for replay safety. Do not infer or reverse historical affection inflation. Do not delete or deduplicate existing history.

## Interfaces

app.migrations.apply_migrations(pool, migration_dir: Path) -> list[str] is async and returns names newly applied by this invocation. main.run_migration() keeps its public no-argument async interface and delegates to it. Errors propagate; startup never labels a real failure as already applied.

## Required behavior

1. Empty fresh database receives the full schema and one seed gift; a second startup makes no changes.
2. Existing 0-100 legacy schema without read_at converts a score of 5 to 50 once.
3. Existing current schema without a ledger preserves a score of 5 and all existing gift/history rows.
4. Concurrent runners execute each migration once.
5. SQL failure leaves no ledger record or partial effects for that transaction and prevents successful startup.
6. Changed or removed applied migrations fail before any new migration executes.
7. Empty/missing migration directories are errors, not successful no-ops.
8. Existing public startup tests are updated to the new intended failure contract.

## Verification and deployment

Use focused unit tests plus real PostgreSQL fixtures against a disposable, loopback-only database from an already-installed image. Never use production DATABASE_URL for these tests. Record before/after score and gift counts in fixtures. After review, deployment requires a private DB backup and before/after aggregate state checks; do not print private content. No live database changes are part of implementation tests.

