-- 190: Her Day — what Klukai chose to wear today, and why
--
-- One row per Commander per local day. base_outfit_id is the outfit she
-- picked for herself (app/wardrobe.py: weather, season, occasions, mood);
-- requested_outfit_id is the one he asked for and she agreed to, which
-- outranks her own pick for the rest of that day. The weather snapshot is
-- the one the day was chosen under, so the duty roster (derived
-- deterministically from day + weather, never stored) stays stable all day.
-- Rows are history: the wardrobe calendar reads them. Never deleted.

CREATE TABLE IF NOT EXISTS companion_her_day (
    user_id             TEXT        NOT NULL,
    day                 DATE        NOT NULL,
    base_outfit_id      TEXT        NOT NULL,
    base_reason         TEXT        NOT NULL DEFAULT '',
    requested_outfit_id TEXT,
    request_changes     INTEGER     NOT NULL DEFAULT 0,
    weather             JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, day)
);
