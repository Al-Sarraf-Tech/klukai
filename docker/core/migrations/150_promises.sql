-- 150: Promises & gentle accountability.
-- Klukai detects commitments the Commander makes ("I'll…", "tomorrow I'll…")
-- and schedules a caring follow-up. A proactive job (wired into engine.py by
-- the orchestrator) reads due_promises() and, after delivery, marks them sent;
-- a resolve endpoint closes the loop with the Commander's response + sentiment.
-- Insert-only from the app, like companion_exchanges (migration 140).

CREATE TABLE IF NOT EXISTS companion_promises (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            TEXT NOT NULL DEFAULT 'jalsarraf',
    promise_text       TEXT NOT NULL,
    commitment         JSONB NOT NULL,
    made_at            TIMESTAMPTZ DEFAULT NOW(),
    scheduled_followup TIMESTAMPTZ,
    followup_sent_at   TIMESTAMPTZ,
    response_text      TEXT,
    resolved_at        TIMESTAMPTZ,
    sentiment          TEXT,
    created_at         TIMESTAMPTZ DEFAULT NOW()
);

-- Open promises for a user (the dashboard / "what did I commit to" view).
CREATE INDEX IF NOT EXISTS idx_comp_promises_user_open
    ON companion_promises(user_id) WHERE resolved_at IS NULL;

-- The scheduler's hot path: due, unresolved follow-ups ordered by time.
CREATE INDEX IF NOT EXISTS idx_comp_promises_due
    ON companion_promises(scheduled_followup) WHERE resolved_at IS NULL;
