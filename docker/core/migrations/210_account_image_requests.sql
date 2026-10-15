-- 210: journal for explicit account chat/API pictures.
-- Written before any native provider call. Recovery reads this row and the
-- worker cache; it does not authorize another render.

CREATE TABLE IF NOT EXISTS companion_account_image_requests (
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    prompt TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    include_commander BOOLEAN NOT NULL,
    phase TEXT NOT NULL,
    memory_id TEXT,
    failure TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_account_image_user_created
    ON companion_account_image_requests (user_id, created_at DESC);
