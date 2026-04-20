-- 100: Audit chain hash column
-- Enables HMAC chain verification — each audit row hashes the previous row's hash.

ALTER TABLE companion_audit_log
    ADD COLUMN IF NOT EXISTS chain_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_audit_chain_hash ON companion_audit_log (chain_hash)
    WHERE chain_hash IS NOT NULL;
