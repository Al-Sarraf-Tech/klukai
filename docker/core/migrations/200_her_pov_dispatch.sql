-- Dispatch attempts are distinct from pipeline progress/generation attempts.
-- Nullable with no backfill: never rewrite existing history or updated_at.
ALTER TABLE companion_her_pov_jobs ADD COLUMN IF NOT EXISTS last_enqueued_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_her_pov_dispatch
    ON companion_her_pov_jobs (last_enqueued_at, created_at)
    WHERE status = 'queued' AND claimed_at IS NULL AND attempts = 0 AND finished_at IS NULL;
