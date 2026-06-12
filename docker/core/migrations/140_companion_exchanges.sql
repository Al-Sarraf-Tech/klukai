-- 140: PostgreSQL fallback store for conversation exchanges (SACRED).
-- Mirrors the companion_episodes fallback: when Qdrant or the embedding
-- service is down, store_exchange lands the raw text pair here instead of
-- losing it. Insert-only from the app; a later job can re-vectorize.

CREATE TABLE IF NOT EXISTS companion_exchanges (
    id                UUID PRIMARY KEY,
    conversation_id   UUID,
    user_content      TEXT NOT NULL,
    assistant_content TEXT NOT NULL,
    topics            TEXT[] DEFAULT '{}',
    mood              TEXT DEFAULT 'composed',
    importance        REAL DEFAULT 0.5,
    user_id           TEXT NOT NULL DEFAULT 'jalsarraf',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_comp_exchanges_time ON companion_exchanges(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_comp_exchanges_user ON companion_exchanges(user_id);
