# ADR-0012: Memory archive seeding cadence — every 2 days, 3-6 AM dominus-local

- **Date:** 2026-04 (formalized 2026-05-16)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

Klukai's "memory archive" is her curated photo album (per
`project_memory_archive.md`): images she's chosen to keep, with
annotations describing the scene + mood. Building this requires:

1. Browsing all generated images (`companion_memories`).
2. Selecting which to keep (`gpt-oss-20b` decides per
   `feedback_gptoss_for_memories.md`).
3. Annotating the kept ones (`dolphin-24b` writes the descriptions).

Steps 2 + 3 are LLM-heavy. Running them during chat hours would
contend with the conversational chat path on the same dominus GPU.

## Decision

Memory archive seeding runs on a **systemd timer on dominus** every
2 days, 3-6 AM (local) when chat traffic is essentially zero.
The cadence + window is captured in `feedback_memory_seeding_schedule.md`
with priority ABSOLUTE.

Pipeline (two-pass):
1. `gpt-oss-20b` selects images from the unprocessed pool. Output:
   list of `memory_id` to keep.
2. `dolphin-24b` annotates each kept image. Output: annotation +
   scene tags.

Reannotation of existing memories runs as a separate path
(`docker/core/reannotate_existing.py`) — useful when annotation
quality bar shifts (e.g., new prompt).

## Consequences

- **No contention with chat**: 3-6 AM is reliably idle for klukai.
- **2-day cadence** means at most ~48h between new image gen and it
  appearing in the archive. Acceptable for a curated album, not
  acceptable for real-time UI.
- **Backfill via `/api/memories/backfill-annotations`**: operator
  can trigger reannotation on-demand without waiting for the
  scheduled run.
- **dominus must be on** for seeding to run. If dominus is off
  during the window, the next run picks up everything.
- **Model selection matters**: per `feedback_dolphin_for_annotations.md`,
  Dolphin for creative text (the annotation IS creative writing),
  gpt-oss for JSON (the selection is structured), gemma-4 for quick
  fixes. NEVER thinking models for creative text.

## Alternatives considered

- **Real-time annotation on image gen**: rejected — would block chat
  while annotation runs. Latency unacceptable.
- **Daily cadence**: rejected — 24h is too frequent; pipeline takes
  ~10-30min per run; weekly is too sparse. 2 days is the trade-off.
- **Manual selection**: rejected — defeats the "Klukai curates"
  framing of `project_memory_archive.md`.

## Related

- `docker/core/seed_memories.py` (main seeding pipeline)
- `docker/core/reannotate_existing.py` (on-demand reannotation)
- `app/memory_archive.py`
- `feedback_memory_seeding_schedule.md` (global CLAUDE.md, ABSOLUTE)
- `feedback_gptoss_for_memories.md`
- `feedback_dolphin_for_annotations.md`
- `project_memory_archive.md`
- ADR-0004 (LM Studio routing — same models)
- ADR-0006 (image gen pipeline — source of memories)
