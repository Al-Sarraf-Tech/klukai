-- 010_companion.sql
-- Companion AI tables in existing aichat database

CREATE TABLE IF NOT EXISTS companion_conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at    TIMESTAMPTZ,
    summary     TEXT,
    mood_start  TEXT DEFAULT 'neutral',
    mood_end    TEXT DEFAULT 'neutral',
    turn_count  INTEGER DEFAULT 0,
    model_used  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS companion_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES companion_conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL,
    content_type    TEXT DEFAULT 'text',
    mood            TEXT DEFAULT 'neutral',
    tool_calls      JSONB,
    tokens_used     INTEGER DEFAULT 0,
    model           TEXT DEFAULT '',
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_comp_msg_conv ON companion_messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comp_msg_created ON companion_messages(created_at DESC);

CREATE TABLE IF NOT EXISTS companion_episodes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES companion_conversations(id),
    summary         TEXT NOT NULL,
    keywords        TEXT[] DEFAULT '{}',
    emotion_tags    TEXT[] DEFAULT '{}',
    importance      REAL DEFAULT 0.5,
    embedding_id    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_comp_episodes_time ON companion_episodes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_comp_episodes_importance ON companion_episodes(importance DESC);

CREATE TABLE IF NOT EXISTS companion_relationship (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS companion_scheduled (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger_type TEXT NOT NULL,
    trigger_spec TEXT NOT NULL,
    action      JSONB NOT NULL,
    enabled     BOOLEAN DEFAULT true,
    last_fired  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
