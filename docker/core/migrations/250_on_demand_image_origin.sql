-- Legacy jobs have no proof of a human picture request. Preserve every row,
-- but require another tap before spending on any never-started queued job.
ALTER TABLE companion_her_pov_jobs
    ADD COLUMN IF NOT EXISTS requested_by_user BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE companion_her_pov_jobs
SET status = 'failed', phase = 'confirmation_required',
    message = 'Pictures are on demand only. Tap Her POV to request a picture.',
    error = 'human_request_required', finished_at = NOW(), updated_at = NOW()
WHERE requested_by_user IS FALSE AND status = 'queued'
    AND attempts = 0 AND claimed_at IS NULL AND finished_at IS NULL;
