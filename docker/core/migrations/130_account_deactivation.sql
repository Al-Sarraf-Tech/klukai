-- Account soft-deactivation support.
-- Moved out of the /api/account/deactivate request path (was a per-request
-- ALTER TABLE taking an ACCESS EXCLUSIVE lock on every call).
ALTER TABLE companion_users ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ;
