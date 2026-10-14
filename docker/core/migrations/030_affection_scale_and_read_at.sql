-- read_at is the historical schema marker for the 0-100 -> 0-1000 conversion.
-- Preserve low scores in current pre-ledger databases; never guess a repair.
-- The conversion and marker commit together in the migration transaction.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'companion_messages'::regclass
          AND attname = 'read_at' AND NOT attisdropped
    ) THEN
        UPDATE companion_affection SET score = score * 10 WHERE score <= 100;
        ALTER TABLE companion_messages ADD COLUMN read_at TIMESTAMP;
    END IF;
END
$$;
