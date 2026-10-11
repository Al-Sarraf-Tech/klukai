-- 200: annotation history on the memory archive
--
-- reannotate_existing.py --rewrite replaces a seed memory's annotation with
-- one written from the real exchange. Her memories are append-only: the text
-- being replaced is pushed onto annotation_history as
-- {"annotation": <old>, "replaced_at": <iso>} in the same UPDATE, so no
-- journal entry is ever lost. Entries are never removed; rows are never deleted.

ALTER TABLE companion_memories
    ADD COLUMN IF NOT EXISTS annotation_history JSONB NOT NULL DEFAULT '[]'::jsonb;
