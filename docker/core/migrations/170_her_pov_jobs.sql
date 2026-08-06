-- 170: durable Her POV job board
--
-- Her POV used an in-process asyncio task + dict for status, dedupe, and
-- progress. That shape is what caused the cancellation wedge, unbounded board,
-- double-render race, and "work lost on restart" class of bugs. Postgres is
-- now the source of truth for the job row; RabbitMQ (klukai.jobs.her_pov,
-- quorum, prefetch=1) only carries the id. The events-bridge owns the queue
-- side so companion-core still speaks no AMQP.

CREATE TABLE IF NOT EXISTS companion_her_pov_jobs (
    id               UUID PRIMARY KEY,
    user_id          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued',
    phase            TEXT NOT NULL DEFAULT 'queued',
    message          TEXT,
    error            TEXT,
    title            TEXT,
    annotation       TEXT,
    mood             TEXT,
    memory_id        TEXT,
    has_image        BOOLEAN NOT NULL DEFAULT FALSE,
    exchange_preview JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT
);

-- At most one non-terminal job per user — DB-level dedupe so a double-tap
-- cannot buy two LLM calls + two GPU renders even across workers.
CREATE UNIQUE INDEX IF NOT EXISTS idx_her_pov_one_active
    ON companion_her_pov_jobs (user_id)
    WHERE status NOT IN ('done', 'failed');

CREATE INDEX IF NOT EXISTS idx_her_pov_queued
    ON companion_her_pov_jobs (created_at)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_her_pov_user_created
    ON companion_her_pov_jobs (user_id, created_at DESC);
