-- 240: per-user settings
--
-- One row per Commander, created on his first change; a missing row means
-- every default. voice_language is the language she SPEAKS when he taps to
-- hear a line (owner decision 2026-10-19): 'en' reads her reply as written,
-- 'ja' has it translated into Japanese first. Fish voice only.

CREATE TABLE IF NOT EXISTS companion_user_settings (
    user_id         TEXT        PRIMARY KEY,
    voice_language  TEXT        NOT NULL DEFAULT 'en'
                                CHECK (voice_language IN ('en', 'ja')),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
