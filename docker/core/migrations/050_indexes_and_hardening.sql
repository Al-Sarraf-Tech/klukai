-- Performance indexes for companion tables
-- These prevent full table scans on frequent queries

-- Messages: queried by conversation_id + created_at constantly
CREATE INDEX IF NOT EXISTS idx_messages_conv_created
    ON companion_messages (conversation_id, created_at DESC);

-- Messages: queried by role (user messages for read receipts)
CREATE INDEX IF NOT EXISTS idx_messages_conv_role
    ON companion_messages (conversation_id, role);

-- Messages: queried by date for daily recaps
CREATE INDEX IF NOT EXISTS idx_messages_created_date
    ON companion_messages (created_at);

-- Messages: read_at NULL filter for unread receipts
CREATE INDEX IF NOT EXISTS idx_messages_unread
    ON companion_messages (conversation_id, role, read_at)
    WHERE read_at IS NULL;

-- Episodes: queried by conversation_id
CREATE INDEX IF NOT EXISTS idx_episodes_conv
    ON companion_episodes (conversation_id);

-- Affection log: queried by timestamp for history
CREATE INDEX IF NOT EXISTS idx_affection_log_created
    ON companion_affection_log (created_at DESC);

-- Conversations: queried by started_at
CREATE INDEX IF NOT EXISTS idx_conversations_started
    ON companion_conversations (started_at DESC);
