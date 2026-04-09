-- 060_memory_archive.sql
CREATE TABLE IF NOT EXISTS companion_memories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filename        TEXT NOT NULL,
    thumb_filename  TEXT,
    prompt          TEXT,
    annotation      TEXT,
    scene_tags      TEXT[] DEFAULT '{}',
    mood            TEXT,
    affection_level INT,
    kept            BOOLEAN DEFAULT true,
    kept_by         TEXT DEFAULT 'klukai',
    category        TEXT DEFAULT 'Mission Records',
    conversation_id TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memories_kept ON companion_memories(kept) WHERE kept = true;
CREATE INDEX IF NOT EXISTS idx_memories_category ON companion_memories(category) WHERE kept = true;
CREATE INDEX IF NOT EXISTS idx_memories_tags ON companion_memories USING gin(scene_tags) WHERE kept = true;
CREATE INDEX IF NOT EXISTS idx_memories_created ON companion_memories(created_at DESC) WHERE kept = true;
