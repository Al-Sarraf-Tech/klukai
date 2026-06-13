-- 151: Voice letters — async JP voice notes Klukai leaves while the Commander
-- is away. When reflection-on-return composes a greeting, it is also synthesized
-- to a .wav (stored under AUDIO_DIR=/audio) and recorded here so the client can
-- play it back as a "voice letter". FAIL-SOFT: if the voice service is down the
-- row is never written and the text greeting path is used unchanged.
--
-- Mirrors the companion_exchanges style (140): UUID PK, user-scoped, insert-only
-- from the app, with a created_at DESC index for "latest" lookups.

CREATE TABLE IF NOT EXISTS companion_voice_notes (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    audio_filename   TEXT NOT NULL,
    text_prompt      TEXT NOT NULL,
    kind             TEXT DEFAULT 'reflection',
    mood             TEXT,
    affection_level  INT,
    conversation_id  UUID,
    user_id          TEXT NOT NULL,
    played_at        TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_comp_voice_notes_user ON companion_voice_notes(user_id);
CREATE INDEX IF NOT EXISTS idx_comp_voice_notes_time ON companion_voice_notes(created_at DESC);
