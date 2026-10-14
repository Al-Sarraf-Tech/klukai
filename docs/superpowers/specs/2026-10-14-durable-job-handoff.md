# Responsive bridge and recoverable queued work

## Problem

The bridge's sole AMQP loop blocks for up to 900 seconds running Her POV over HTTP while negotiating 30-second heartbeats. A Redis subscriber receipt is also mistaken for durable AMQP acceptance; a crash between those steps strands the DB job.

## Intended design

The PostgreSQL job row is the durable accepted-work record. In production queue mode, insertion must succeed before success is returned. A Redis publication is only a wake-up hint. A small core reconciler republishes only unstarted `queued` jobs by their existing UUID until the worker claims them. Bound batch size and retry cadence using a new nullable `last_enqueued_at` column (migration200), without changing existing historical rows or `updated_at` used by pipeline status. Mark dispatch attempt before publication; crash/failure then waits at most one retry interval. Run an immediate reconciliation at startup and a cancellable loop thereafter. Do not re-enqueue started or ambiguous cloud work, reset attempts or generate new job IDs.

Keep at most one active Her POV HTTP request per bridge process, in a worker thread with existing urllib timeout. AMQP channel access belongs exclusively to its main thread. The main loop services heartbeats, Redis forwarding and deferred work on every iteration including busy/special-event paths. Completed HTTP results are applied by the main thread, only to the originating live channel generation. After reconnect, never ack/nack an old delivery tag on a new channel. Keep the active worker until it completes so reconnect does not start a concurrent job. The durable core claim still prevents duplicate execution. Worker failure/requeue handling is bounded and must not busy-loop.

Use publisher confirms and mandatory routing for durable job handoff (and deferred re-parking if sharing confirmation mode). Do not claim confirmations provide end-to-end exactly-once execution. Invalid messages are handled explicitly. Acknowledgment/rejection logic must distinguish malformed or nonexistent jobs from transient core/auth infrastructure failures; a transient authorization/config failure must not silently discard durable work. Ordinary event failure remains bounded with honest logs.

Deferred HTTP callbacks must also not monopolize the owner loop: use a separate bounded worker or service AMQP while waiting with bounded due batches. A blocked due callback cannot prevent Her POV completion handling or heartbeats. Preserve existing deferred DB idempotency and keep all channel calls on the owner thread.

Production cloud work remains queue-only; no fallback to inline or GPU, no cloud claim takeover after ambiguous timeout. Broker/process restart may redeliver the same UUID but must never produce a new generation attempt after an already-started cloud claim. Cancellation/shutdown must close loops/threads safely without pretending an unfinished provider outcome is known.

## Acceptance

Hold a fake Her POV HTTP call beyond several simulated heartbeat intervals while asserting heartbeat ticks, ordinary event forwarding, deferred delivery and no second job start. Reconnect during the held call and prove old tags are not acknowledged on the replacement channel. Exercise publish rejection/unroutable jobs, disconnect before confirmation, repeated job delivery, malformed bodies, HTTP auth/server failures, and result completion after reconnect. Core tests cover crash after DB insert before publication, Redis subscriber plus failed downstream publish, startup recovery and retry throttling, concurrent reconcilers, and strict exclusion of started/terminal cloud jobs. No real model, GPU, production DB/broker or paid request.
