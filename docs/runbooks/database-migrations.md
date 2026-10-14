# Database migrations

Klukai startup calls `app.migrations.apply_migrations` through the existing
`main.run_migration()` interface. The runner reads the packaged UTF-8 `.sql`
files in lexical filename order and computes SHA-256 over their exact bytes.
It acquires one PostgreSQL transaction-scoped advisory lock, validates the
entire recorded history, and applies all pending files and ledger entries in
one transaction. Success returns the newly applied filenames. An empty or
missing migration directory, changed or missing recorded file, SQL error, or
lock timeout prevents startup. No failed migration is reported as successful.

The ledger is `companion_schema_migrations(name, sha256, applied_at)`, with
`name` as its primary key. The lock ID is `5425801856799755591`, shared across
all runners in the database. `SET LOCAL lock_timeout = '30s'` bounds advisory
and schema lock waits; the setting and lock disappear at transaction end.
This is not a general SQL execution timeout. Concurrent deployments must use
the same migration runner and immutable packaged files.

## First adoption of an existing database

A database without the ledger replays the guarded historical scripts once,
then records them atomically. Migration 030 uses the existing
`companion_messages.read_at` column as the historical conversion marker. If
absent, it multiplies scores at or below 100 by ten and adds `read_at` in the
same transaction. If present, scores are untouched, including low scores.
There is no attempt to infer or repair past score inflation. An unusual manual
schema change that removed this marker requires investigation before rollout.

Migration 080 inserts the canonical Klukadile plush only when no matching
`user_id = 'jalsarraf'` and `item = 'Klukadile plush'` row exists. Existing
duplicate gifts remain intact. This intentionally adds no uniqueness constraint
and deletes no history.

The other historical scripts were reviewed: 010/050/060/090/100/110/130/140/
150/151/160/170/180/190 use guarded table, column and index creation. The 020
and 040 seeds have primary-key conflict guards; 070 adds guarded columns and
indexes and only fills a null affection user ID; 120 uses a user-key conflict
guard when backfilling subscriptions. This review establishes replay safety
for the shipped schemas, not validation of every manually altered schema.
Incompatible existing definitions still cause startup SQL errors.

## Release and backup procedure

Before the first production adoption, take a private PostgreSQL backup using
the existing operational credentials and backup location. Protect it with owner
only access, verify successful completion, and verify restoration in a separate
disposable database. Never print credentials, messages, or private row contents
into deployment logs. This implementation's tests do not authorize a deployment.

Record before/after aggregate checks for affection row counts and score totals,
gift counts, message/history counts, and schema/ledger state. For a current
schema, adoption must preserve existing affection scores and gifts. Compare
more than totals if an aggregate differs; do not silently modify history to
make a check pass. Confirm a second startup applies zero files and preserves
those checks. Keep the backup and pre-rollout version identity together.

## Failed startup diagnosis

- Missing/empty directory: check the container build included the migration
  directory and all release files. Do not start with migration checks disabled.
- Changed or missing applied file: compare the packaged release with the
  ledger's filenames and checksums. Restore the original recorded file bytes;
  implement subsequent changes as a new migration. Do not edit ledger hashes
  to bypass the error.
- Lock timeout: inspect PostgreSQL activity and locks for the competing startup
  or long-running transaction. Let it finish or resolve it through the normal
  operations process, then retry startup. Never remove the serialization lock.
- SQL failure: inspect the failing migration and database error privately.
  All pending work and its ledger entries roll back together. Already committed
  migrations from earlier startups remain intact. Correct the unapplied file
  and retry; do not swallow the error or mark an unapplied file as applied.

The runner logs only committed migration names, not SQL bodies or database
credentials. Database exceptions can include SQL context; handle diagnostic
logs as private operational evidence.

## Adding migrations and rolling back

Add a new lexically later `.sql` file. Treat recorded scripts as immutable,
including comments, encoding, and line endings. Use transactional SQL only:
no `BEGIN`, `COMMIT`, `ROLLBACK`, explicit transaction termination inside
procedures, or commands such as `CREATE INDEX CONCURRENTLY` that require their
own transaction. Do not modify the ledger or migration lock from a script.
Review data preservation and test forward application and restart behavior.

Prefer a state-preserving forward fix. Never roll back to the old replaying
startup loop: it can inflate affection and add gifts again. An older code
release lacking newly recorded migration files deliberately fails the history
check. Application rollback therefore needs a compatible runner plus the full
recorded migration set, or a separately reviewed compatibility release. Do
not delete ledger rows, reverse affection scores, deduplicate gifts, or restore
a backup over newer conversations merely to make an old release boot. A
backup restore loses writes after its snapshot and requires a separately
approved recovery plan.

## Disposable PostgreSQL verification

Tests read only `KLUKAI_MIGRATION_TEST_DATABASE_URL`; they never use the
application's `DATABASE_URL`. Supply a disposable, loopback-only database.
Each test creates a random `migration_test_*` schema and drops only that schema
in a `finally` cleanup. Without the dedicated variable, PostgreSQL tests skip.
The database role needs schema creation permission. Never point it at production.

```sh
KLUKAI_MIGRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:PORT/klukai_migration_test \
  python3 -m pytest docker/core/tests/test_migrations.py \
  docker/core/tests/test_migrations_postgres.py docker/core/tests/test_main_coverage.py -q
```

Use an already-installed PostgreSQL image pinned by ID, a temporary data mount,
and a random port published only on `127.0.0.1`; remove the disposable container
afterwards. The lock-timeout case intentionally takes about 30 seconds. The
suite covers fresh installation, both legacy schema states, duplicate/history
preservation, byte checksums, missing/changed files, transaction rollback,
concurrent runners, timeout recovery, and startup error propagation.
