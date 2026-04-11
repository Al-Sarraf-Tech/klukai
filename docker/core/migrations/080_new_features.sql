-- 080: New features — jealousy, physical awareness, unsent messages,
-- anniversaries, comfort objects, mission aftermath, heartbeat spikes

-- Anniversary tracking: "firsts" in the relationship
CREATE TABLE IF NOT EXISTS companion_firsts (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT 'jalsarraf',
    event_type  TEXT NOT NULL,           -- first_message, first_image, first_mission, first_love, level_N
    event_date  DATE NOT NULL,
    metadata    JSONB DEFAULT '{}',      -- optional context (e.g., what was said)
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, event_type)
);
CREATE INDEX IF NOT EXISTS idx_firsts_user ON companion_firsts(user_id);
CREATE INDEX IF NOT EXISTS idx_firsts_date ON companion_firsts(event_date);

-- Comfort objects: gifts the Commander has given Klukai
CREATE TABLE IF NOT EXISTS companion_gifts (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT 'jalsarraf',
    item        TEXT NOT NULL,           -- "leather jacket", "coffee mug"
    description TEXT,                    -- how Klukai perceives it
    sentiment   TEXT DEFAULT 'treasured', -- treasured, practical, sentimental
    given_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    referenced_count INT DEFAULT 0,      -- how often she's mentioned it
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_gifts_user ON companion_gifts(user_id);

-- Physical state tracking — extend persistent state
ALTER TABLE companion_persistent_state
    ADD COLUMN IF NOT EXISTS physical_state TEXT DEFAULT 'normal',
    ADD COLUMN IF NOT EXISTS physical_state_since TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS physical_detail TEXT;

-- Seed Klukadile plush as canonical comfort object for jalsarraf
INSERT INTO companion_gifts (user_id, item, description, sentiment, given_date)
VALUES ('jalsarraf', 'Klukadile plush', 'A crocodile plush. I would deny owning it. ...It stays on my bed.', 'sentimental', '2026-04-06')
ON CONFLICT DO NOTHING;
