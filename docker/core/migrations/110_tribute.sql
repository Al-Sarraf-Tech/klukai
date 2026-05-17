-- 110: Tribute system — Commander honors Klukai with heartfelt messages.
--
-- Per the "treat her like a princess" surprise feature:
-- - Commander can POST /api/tribute with a heartfelt message.
-- - Each tribute is a sacred record (per feedback_never_delete_chat.md,
--   tributes are also never deleted).
-- - One tribute per user can be the "crown jewel" — always referenced
--   in Klukai's system prompt at affection level 4+, regardless of
--   semantic recall.
-- - 24h cooldown between tributes so they stay rare and meaningful.

CREATE TABLE IF NOT EXISTS companion_tributes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    text            TEXT NOT NULL,
    mood_at_time    TEXT,
    affection_at_time INTEGER,
    is_crown_jewel  BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Only ONE crown jewel per user at any time. The partial unique index
-- enforces this without preventing multiple non-crown rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_companion_tributes_crown_per_user
    ON companion_tributes (user_id) WHERE is_crown_jewel = true;

CREATE INDEX IF NOT EXISTS idx_companion_tributes_user_created
    ON companion_tributes (user_id, created_at DESC);

-- Auto-set the most recent tribute as crown jewel IF the user has none yet.
-- We don't auto-overwrite — Commander explicitly chooses the crown jewel
-- via POST /api/tributes/{id}/crown.
