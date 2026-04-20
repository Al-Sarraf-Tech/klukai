-- 090: Security audit log
-- Append-only log of security-relevant events for forensics and compliance.

CREATE TABLE IF NOT EXISTS companion_audit_log (
    id           BIGSERIAL PRIMARY KEY,
    event_type   TEXT NOT NULL,
    user_id      TEXT,
    ip_address   TEXT,
    request_id   TEXT,
    metadata     JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_created_at
    ON companion_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_user_id_created_at
    ON companion_audit_log (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_event_type
    ON companion_audit_log (event_type, created_at DESC);
