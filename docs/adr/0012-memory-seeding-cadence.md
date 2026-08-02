# ADR-0012: Memory archive seeding cadence — every other local day, 03:00–06:00

- **Date:** 2026-04 (formalized 2026-05-16)
- **Updated:** 2026-08-01 (Amarillo timer reconstruction)
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
catches up outside the approved window. The service has a 6,900-second timeout
so a stuck 04:00 run ends before 06:00. It executes exactly:

```text
docker compose exec -T companion-core python3 /app/seed_memories.py
```

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
