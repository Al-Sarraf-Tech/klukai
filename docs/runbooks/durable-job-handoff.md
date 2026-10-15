# Durable Her POV handoff

The API accepts queue-mode work only after its PostgreSQL row commits. A Redis
subscriber receipt does not confirm RabbitMQ acceptance. The reconciler commits
`last_enqueued_at` before publishing a wake-up hint and retries the same UUID
no sooner than 30 seconds later. It starts immediately at core startup, then
checks every five seconds, at most 16 rows each pass. Backlogs take additional
passes. Concurrent core instances use row locks with `SKIP LOCKED`.

Only `status = 'queued'`, `claimed_at IS NULL`, `attempts = 0`, and
`finished_at IS NULL` are eligible. Dispatch never changes `updated_at`,
`claimed_at`, `attempts`, or the UUID. Lost Redis hints, rejected/unroutable
AMQP publishes, and crashes between insertion and publication remain eligible
for recovery. These guarantees require the DB row to remain intact.

## Investigate delayed work

Use existing authorized read-only DB and broker access. Inspect the row's UUID,
status, claim/finish timestamps, attempts, and dispatch timestamp. Check bridge
logs for topology, publish-confirm, routing, or HTTP failures; compare the
configured shared internal token on both services without printing it.

- Unstarted queued rows should receive periodic dispatch marks and reach the
  durable queue after Redis/RabbitMQ/core connectivity recovers. Restore the
  failing service/configuration and let reconciliation reuse the UUID.
- Auth failures, rate limits, HTTP 5xx, malformed success responses, and core
  `ok: false` are retryable. The bridge does not silently discard these jobs.
- Malformed queue payloads and explicit malformed-request HTTP 400/422 responses
  are rejected. Unknown well-formed UUIDs return core `ok: true` and are acked.
- Started cloud work is ambiguous after timeout/process loss. Do not set it
  back to queued, clear its claim, reset attempts, or create replacement work
  merely because time elapsed. Establish the provider/archive outcome first.
  Core will not take over a started cloud claim.

One job HTTP slot and one due-callback slot are retained across reconnects.
AMQP stays on the main thread, including confirms, acks and nacks. An old
channel's delivery tag is never used on a replacement channel. Repeated
messages may reach core, but its durable cloud claim prevents starting the
same UUID twice. This is not an end-to-end exactly-once claim.

Shutdown closes the channel and stops the owner loop. Python urllib cannot
cancel an in-progress request safely: daemon HTTP threads retain the existing
900-second job / 15-second due timeout and never touch AMQP after shutdown.
An unfinished request is not marked successful or failed on this basis.
Deferred DB idempotency and the existing core sweeper remain unchanged.

## Release and offline verification

Deploy migration `200_her_pov_dispatch.sql` with core's once-only migration
runner before enabling the new reconciler. It adds a nullable dispatch column
and a partial index, without backfilling historical rows or pipeline progress.
Apply the normal private backup and release checks before any production
rollout. This implementation's development tests did not deploy services or
make provider/GPU requests.

Run bridge tests in a separate pytest process from core:

```sh
python3 -m pytest docker/events-bridge/tests -q
python3 -m pytest docker/core/tests/test_job_handoff.py docker/core/tests/test_memory_her_pov.py docker/core/tests/test_creative_text_paths.py docker/core/tests/test_main_coverage.py -q
```

Bridge tests use controlled threads and transport doubles. When pika is absent,
the harness supplies only a minimal import shim; that is not live AMQP evidence.
Use the already pinned bridge runtime dependency for tests that need actual
pika exception/property classes. Do not install dependencies or contact a live
broker as part of these unit tests.

The SQL tests reuse the migration suite's isolated-schema fixture. Set only
`KLUKAI_MIGRATION_TEST_DATABASE_URL` to an explicitly disposable PostgreSQL
instance, never production `DATABASE_URL`, then run:

```sh
python3 -m pytest docker/core/tests/test_job_handoff_postgres.py docker/core/tests/test_migrations_postgres.py -q
```

Without that dedicated URL the SQL cases skip. An old test-port number is not
proof of a current disposable instance; establish its identity each time.
