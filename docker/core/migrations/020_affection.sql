-- Companion affection system tables
-- Tracks relationship progression between Klukai and the Commander

CREATE TABLE IF NOT EXISTS companion_affection (
    id                   SERIAL PRIMARY KEY,
    score                INTEGER NOT NULL DEFAULT 0,
    level                INTEGER NOT NULL DEFAULT 0,
    level_name           TEXT NOT NULL DEFAULT 'Cold Assessment',
    last_interaction_date DATE,
    consecutive_days     INTEGER NOT NULL DEFAULT 0,
    daily_points_earned  INTEGER NOT NULL DEFAULT 0,
    total_interactions   INTEGER NOT NULL DEFAULT 0,
    first_interaction    TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Singleton row for single-user companion
INSERT INTO companion_affection (id) VALUES (1) ON CONFLICT DO NOTHING;

-- Audit log of all affection changes
CREATE TABLE IF NOT EXISTS companion_affection_log (
    id          SERIAL PRIMARY KEY,
    delta       INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    old_score   INTEGER NOT NULL,
    new_score   INTEGER NOT NULL,
    old_level   INTEGER NOT NULL,
    new_level   INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_affection_log_time
    ON companion_affection_log(created_at DESC);
