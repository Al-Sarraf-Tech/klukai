-- Persistent mood state — survives session expiry
CREATE TABLE IF NOT EXISTS companion_persistent_state (
    user_id TEXT PRIMARY KEY DEFAULT 'default',
    mood TEXT NOT NULL DEFAULT 'composed',
    last_topic TEXT,
    last_conversation_id TEXT,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Seed default row
INSERT INTO companion_persistent_state (user_id)
VALUES ('default')
ON CONFLICT DO NOTHING;
