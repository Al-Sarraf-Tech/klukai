# Klukai pictures are on demand only

The owner's final hard rule supersedes earlier automatic/nightly picture
allowances: no automatic pictures, including dream pictures. Dreams remain
text. This source change builds on fal-only commit `d509fe3` and is not deployed.

Automatic entry points are inert regardless of stale environment caps:

| Trigger | Source behavior |
| --- | --- |
| Wardrobe changes, portrait prewarm, idle portrait backfill | No work enqueued |
| `GET /api/portrait` | Cached signed frames only; an already requested manual render still reports pending/partial |
| Spontaneous-art schedule and direct event invocation | No render, archive write or gift announcement |
| Mission completion picture hook | No render or picture announcement; decompression and physical-state handling remain |
| Deferred `her_pov` | New schedules rejected before persistence; old deferred rows ignored |
| Album seed service / script | Successful no-op before reading history or calling any provider |
| Night dreams and reflection dreams | Existing text-only writers preserved |

Explicit chat pictures, `POST /api/generate-image`, the Her POV human CTA,
and supported manual portrait refresh retain their rendering helpers. Provider,
model, prompt and character intimacy behavior is unchanged. The fal renderer
keeps provider safety checks enabled and refuses unsupported nudity-grant
requests before reading references, accessing credentials or reserving spend.

Migration `250_on_demand_image_origin.sql` records immutable human origin for
new Her POV requests. Worker and recovery SQL require that origin. Legacy
never-started queued jobs lack provenance, so the migration preserves their
rows and annotations but marks only those jobs failed/confirmation_required,
with `human_request_required`; another human tap can create a fresh request.
Started or ambiguous jobs are untouched and are not newly submitted. Progress
upserts cannot grant origin to a legacy job or remove it from a human request.

The owner reports the existing Klukai fal key was added to encrypted storage
and backed up. No key value or credential file was inspected for this work.
Each companion must use its own encrypted credential store; no new keys,
top-ups or paid test calls are authorized by this handoff.

Owner clarification (2026-10-23): Commander scenes must not use his portrait in
image renders. The renderer uses only Klukai's approved image reference and a
text-only description of the Commander: adult human man, short dark crew-cut
hair, brown eyes, light-olive skin with natural warm shading, mature masculine
facial features, and a strong build. His portrait is never opened or sent to
the image provider.

The owner subsequently authorized deployment of both apps' updates. The
coordinating agent owns that deployment; this branch remains source preparation
until the coordinator deploys it. No production checkout, services, credentials,
conversations, memories or image assets were changed during this preparation.

Validation: 6,596 offline core tests passed (96 optional/live tests skipped,
one performance test deselected). Coverage includes all `app` modules:
98.07% statements, 95.42% branches, 97.49% combined; no coverage exclusions
were added. The 24 migration/durable-queue tests passed against a disposable
PostgreSQL 16 container with no production mounts; it was removed afterward.
The Amarillo operations suite passed 756 tests, including 32 mocked fal tests;
the focused fal suite also passed with warnings treated as errors. Ruff
application check/format and mypy (173 files) passed. Independent review found
no outstanding critical or important issues. No real image or paid API request
was made.
