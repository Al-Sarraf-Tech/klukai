-- 160: scheduler durability
--
-- Two separate concerns that were previously conflated by having neither:
--
--   companion_job_runs  — bookkeeping for the *recurring* APScheduler jobs, so a
--                         fire time that passes while the process is down can be
--                         detected and replayed on startup instead of vanishing.
--
--   companion_scheduled — the *one-shot deferred* tasks ("remind him in 3 hours").
--                         The table already existed with exactly this shape but
--                         was never written to or read from; these columns make
--                         it the durable source of truth behind the RabbitMQ
--                         delay rail, so a broker outage delays work rather than
--                         losing it.

-- ── Recurring job bookkeeping ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS companion_job_runs (
    job_id      TEXT PRIMARY KEY,
    last_fired  TIMESTAMPTZ NOT NULL,
    last_status TEXT NOT NULL DEFAULT 'ok',   -- ok | error | caught_up
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── One-shot deferred tasks ─────────────────────────────────────────────────

ALTER TABLE companion_scheduled
    ADD COLUMN IF NOT EXISTS due_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS status       TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS attempts     INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_error   TEXT;

-- The sweeper's only query: pending work that is already due. Partial index so
-- it stays small no matter how much history accumulates.
CREATE INDEX IF NOT EXISTS idx_scheduled_due
    ON companion_scheduled (due_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_scheduled_user_status
    ON companion_scheduled (user_id, status);
