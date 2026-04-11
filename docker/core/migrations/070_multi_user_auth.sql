-- 070: Multi-user authentication with complete data isolation
-- Each user gets their own isolated data silo. Zero sharing between users.

-- Authentication: users table with bcrypt hashes
CREATE TABLE IF NOT EXISTS companion_users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT DEFAULT 'Commander',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- IP ban tracking for brute force protection
CREATE TABLE IF NOT EXISTS companion_login_attempts (
    ip_address TEXT NOT NULL,
    attempted_at TIMESTAMPTZ DEFAULT NOW(),
    success BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON companion_login_attempts(ip_address, attempted_at);

-- Auth session tokens
CREATE TABLE IF NOT EXISTS companion_auth_sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES companion_users(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT NOW() + INTERVAL '7 days'
);

-- Add user_id to all companion tables
ALTER TABLE companion_affection ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT 'jalsarraf';
ALTER TABLE companion_affection_log ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT 'jalsarraf';
ALTER TABLE companion_conversations ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT 'jalsarraf';
ALTER TABLE companion_episodes ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT 'jalsarraf';
ALTER TABLE companion_memories ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT 'jalsarraf';
ALTER TABLE companion_messages ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT 'jalsarraf';
ALTER TABLE companion_relationship ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT 'jalsarraf';
ALTER TABLE companion_scheduled ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT 'jalsarraf';

-- Create indexes for user_id filtering
CREATE INDEX IF NOT EXISTS idx_conv_user ON companion_conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_msg_user ON companion_messages(user_id);
CREATE INDEX IF NOT EXISTS idx_mem_user ON companion_memories(user_id);
CREATE INDEX IF NOT EXISTS idx_ep_user ON companion_episodes(user_id);
CREATE INDEX IF NOT EXISTS idx_aff_log_user ON companion_affection_log(user_id);
CREATE INDEX IF NOT EXISTS idx_rel_user ON companion_relationship(user_id);

-- Make affection per-user (drop singleton id=1 pattern)
-- The existing row becomes jalsarraf's
UPDATE companion_affection SET user_id = 'jalsarraf' WHERE user_id IS NULL;
