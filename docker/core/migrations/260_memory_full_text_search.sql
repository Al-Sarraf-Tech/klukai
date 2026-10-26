-- 260: full-text recall for episodic and exchange memory
--
-- Owner decision 2026-10-26: Klukai runs on cloud AI only. Memory recall no
-- longer uses local embeddings or a vector store: Postgres ranks a user's rows
-- by a full-text match on their stored text, then importance and recency, and
-- Claude Haiku picks the relevant ones from those candidates. Nothing is
-- deleted or re-embedded; existing rows get their search text computed here.
--
-- The tsvector is a stored generated column so a recall never re-parses text.
-- array_to_string is only STABLE (element output can depend on settings); for
-- text[] it is immutable, which this wrapper declares so it can feed a
-- generated column. Replay-safe: every statement is idempotent.

CREATE OR REPLACE FUNCTION companion_text_array_join(items TEXT[])
RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$ SELECT coalesce(array_to_string(items, ' '), '') $$;

ALTER TABLE companion_episodes
    ADD COLUMN IF NOT EXISTS search_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'english'::regconfig,
            coalesce(summary, '') || ' ' || companion_text_array_join(keywords)
        )
    ) STORED;

ALTER TABLE companion_exchanges
    ADD COLUMN IF NOT EXISTS search_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'english'::regconfig,
            left(coalesce(user_content, ''), 4000) || ' '
                || left(coalesce(assistant_content, ''), 4000) || ' '
                || companion_text_array_join(topics)
        )
    ) STORED;
