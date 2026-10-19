# ADR-0012: Memory archive seeding cadence — every other local day, 03:00–06:00

- **Date:** 2026-04 (formalized 2026-05-16)
- **Updated:** 2026-08-01 (Amarillo timer reconstruction); 2026-10-11 (in-container deadline)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

Klukai's memory archive is her curated photo album. The seed pipeline selects
meaningful unprocessed conversations, writes in-character annotations, and
uses the image pipeline for retained memories. It needs the live
`companion-core` container and database on Amarillo, while its LLM and ComfyUI
calls consume the shared RTX 3090 through the authenticated Tailscale gateway
and bounded GPU lease on `dominus-nobara`.

The original schedule was associated with the lost `dominus` environment and
had no surviving unit in this repository. Restoring that dead host as a timer
owner would make the schedule operationally false.

## Decision

Amarillo owns `klukai-memory-archive-seed.timer` and its oneshot service. The
timer evaluates daily at 04:00 America/Chicago. A checked `ExecCondition`
admits only even Unix-epoch local calendar days, yielding a deterministic
every-other-day cadence, and rejects any execution outside 03:00–06:00.

The timer has `Persistent=false`, so a machine that was down at 04:00 never
catches up outside the approved window. A 04:00 run is bounded inside the
window three times over: the seeder stops itself after `SEED_DEADLINE_SECONDS`
(6,000 s, 05:40) without advancing `last_seeded_at`; `SEED_HARD_KILL_SECONDS`
(6,600 s) arms SIGALRM in the seeder itself, ending it inside the container at
05:50 even mid-call (the distroless image has no coreutils `timeout`; the unit's own
`TimeoutStartSec` reaches only the `docker compose exec` client, so before this
a stuck seeder survived it and kept the GPU past 06:00); and the service's
6,900-second `TimeoutStartSec` remains the outer bound. It executes exactly:

```text
docker compose exec -T -e SEED_DEADLINE_SECONDS=6000 \
  -e SEED_HARD_KILL_SECONDS=6600 companion-core python3 /app/seed_memories.py
```

Each run reads the messages after its watermark in `(created_at, id)` order
and pairs them with `app/seed_exchanges.py`, one timestamp group at a time: a
reply answers the newest unanswered user message from an earlier timestamp,
never one at its own, so the random order of a tie cannot mispair anything.
It takes the oldest `SEED_MAX_EXCHANGES` (default 40) exchanges. A handled
exchange is one whose selection batch answered and, if it was chosen, whose
memory reached a durable save or was a confirmed duplicate (fingerprint
`seed:<user message id>`, unique even when timestamps tie). Whatever ends a
run (the deadline, an outage, a failed render), the watermark moves over the
handled leading exchanges and stops at the first unfinished one: it records
the last handled reply's timestamp plus the "carry", the newest user message
at that timestamp still waiting for its reply, which the next run reads
again. Inside the soft deadline, selection stops at 30% of the budget and
annotation at 60%, so the leading annotated exchanges are always rendered and
saved; only annotated picks are rendered. A backlog therefore always drains,
exchanges are never split or re-paired across runs, and ties are neither
skipped nor re-read. The watermark is stored as `last_seeded_at:<user>` (the
bare timestamp older seeders read) plus `last_seeded_carry:<user>`
(`{"created_at", "carry"}`, used only while its timestamp matches), written
in one commit.

Model aliases remain application policy in `docker/core/seed_memories.py` and
must resolve through `ops/dominus-nobara/models.lock.json`. The timer neither
overrides model choice nor bypasses Tailscale, bearer authentication, the game
guard, or the GPU lease.

## Consequences

- The schedule follows the host that owns `companion-core` and its database.
- There is no boot-time or daytime catch-up. A missed or failed eligible run is
  picked up by the pipeline's unprocessed-state logic on a later eligible date.
- Amarillo and the Dominus Tailnet services must both be healthy for GPU-backed
  selection/image work; failure does not permit a LAN or dead-host fallback.
- Operators do not manually start the service outside the approved window.
- During a GPU embargo, the checked units may be installed and statically
  verified, but the timer remains disabled and inactive.

## Alternatives considered

- **Timer on dominus-nobara:** rejected because the core container and database
  live on Amarillo; remote orchestration would add another failure boundary.
- **Persistent catch-up:** rejected because it can run during chat or gaming
  hours and violate the absolute time window.
- **Daily or weekly cadence:** rejected; every other day is the established
  balance between freshness and GPU contention.

## Related

- `ops/amarillo/README.md`
- `ops/amarillo/systemd/klukai-memory-archive-seed.service`
- `ops/amarillo/systemd/klukai-memory-archive-seed.timer`
- `docker/core/seed_memories.py`
- ADR-0004 (locked local model routing)
- ADR-0006 (image generation pipeline)
