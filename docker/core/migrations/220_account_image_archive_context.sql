-- Preserve the original generation's archive provenance across retries.
-- Existing rows have no historical context; do not infer or repair it.
ALTER TABLE companion_account_image_requests
    ADD COLUMN IF NOT EXISTS archive_context JSONB NOT NULL DEFAULT '{}'::jsonb;
