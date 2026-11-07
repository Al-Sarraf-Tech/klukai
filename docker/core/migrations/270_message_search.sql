-- Literal original-message search; additive, with no history cutoff.
-- Measured on synthetic PostgreSQL 17: rare search 647.774 -> 0.906 ms,
-- no-match 698.108 -> 0.271 ms. Two-character searches may still scan rows.
-- The runner is transactional, so this regular GIN build blocks writes while
-- building. Keep public qualification for isolated-schema migration runners.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
CREATE INDEX IF NOT EXISTS idx_messages_content_trgm
    ON companion_messages USING gin (content public.gin_trgm_ops);
