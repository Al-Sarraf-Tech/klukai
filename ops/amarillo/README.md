# Amarillo operations

Amarillo owns Klukai's core services, persistent state, PWA, event bridge, and
the retained [Intel Arc XTTS release](voice_xpu/README.md). The documented
production conversation primary is Sonnet 5.5 with medium adaptive thinking;
Dominus is secondary. Main speech uses Dominus-Nobara XTTS with asynchronous
jobs and 24 kHz WAV output. Arc speech is an explicit rollback choice, with no
automatic Arc or paid voice fallback. Text remains available without Dominus.
Transcription retains its separate Dominus endpoint.

Production images use the private Agy/Grok account pair with alternating
preference and bounded eligible failover. The newly requested Dominus-third,
Amarillo-last image chain is research work, not an activated production route.
See the [account-image guide](../../docs/handoff/2026-10-14-agy-main-images.md),
[main voice handoff](../../docs/handoff/2026-10-14-nobara-primary-voice.md) and
[ordered-backup samples](../../docs/handoff/2026-10-15-ordered-image-backup-research.md).

Fresh checkouts retain local creative defaults and zero creative API allowances.
The owner authorized production Anthropic dream/Her POV writing with 12 attempts
per UTC day and month, shared across both text features. Account-quota images
are separately authorized; paid image API allowances stay zero, Google paid
credit fallback stays disabled and Grok extra usage stays off. The
[creative-provider handoff](../../docs/handoff/2026-10-14-dream-image-preparation.md)
distinguishes API attempts from the later account-image activation.

Operator changes must preserve the independent hosts' admission rules: no
synthetic warmup when local load-on-demand is selected, bounded GPU residency,
Dominus gaming guards, and Arc's thermal/process limits. A core or PWA release
does not authorize starting a GPU service. Read the
[Dominus boot/publish handoff](../../docs/handoff/2026-09-25-dominus-tailscale-publish.md)
before repair or startup work.

## Releases and deploy-on-merge

Core releases are CI-built, relayed, signed and deployed with automatic
rollback ([release runbook](../../docs/runbooks/release.md)). Once the owner
activates it, [deploy-on-merge](../../docs/runbooks/deploy-on-merge.md) does
this on its own for each certified merge to `main`: a two-minute user timer
(`klukai-deploy-on-merge.timer`) runs the owner-pinned controller, which
deploys core and the PWA, including the atomic web root at
`~/.local/share/klukai/web`, then verifies and restores on failure.
`klukai-deploy-staging.py` replays every failure mode against disposable
targets.

## Memory archive seeding

Amarillo owns the memory-seeding schedule because `companion-core`, its
database, and the seed script live there.

Since 2026-10-19 seeding does not need Dominus. With
`KLUKAI_IMAGE_PROVIDER=agy` (production) the seeder selects and annotates
through the background text helper (Claude Haiku, `app/extraction_llm.py`)
and draws each kept memory through the account worker
(`klukai-agy-images.service`), the same journalled path as chat pictures. It
draws nothing unless core's environment sets `KLUKAI_ALBUM_SEED_RENDERS` to
`1` or `2`: that many renders per run and per America/Chicago day, never more
than two. To turn album seeding on, set `KLUKAI_ALBUM_SEED_RENDERS=2` in the
encrypted runtime env, recreate `companion-core`, then enable the timer
below. See ADR-0012 for the recovery and refusal rules.

The legacy pipeline (Dominus LLM + ComfyUI through Klukai's authenticated
Tailscale clients and the bounded GPU lease) runs only with the explicit
`KLUKAI_IMAGE_PROVIDER=comfyui`; the user unit never connects to a LAN or raw
ComfyUI port. In that mode seeding stays local-only: if
`KLUKAI_CREATIVE_TEXT_PROVIDER` or the image provider selects a paid cloud
API, the seed script exits before reading history, acquiring a GPU lease, or
dispatching an API call. Do not work around this refusal by increasing
attempt limits or deleting the ledger.

The timer checks every day at 04:00 America/Chicago. Its `ExecCondition` runs
only on even Unix-epoch calendar days, producing the required deterministic
every-other-local-day cadence. The same condition rejects execution outside
03:00–06:00, including a manual or delayed invocation. `Persistent=false`
prevents boot catch-up. A long run is ended before 06:00 by three nested
bounds: the seeder honours `SEED_DEADLINE_SECONDS=6000` (passed with
`docker compose exec -e`) and stops itself before its next unit of work, leaving
`last_seeded_at` untouched so the range is retried on a later eligible day;
`SEED_HARD_KILL_SECONDS=6600` arms SIGALRM inside the seeder (the distroless
image has no coreutils `timeout`), so the seeder itself is ended (systemd's `TimeoutStartSec` only kills the
`docker compose exec` client, and a seeder left running past 06:00 kept taking
GPU leases while he chatted); `TimeoutStartSec=6900` remains the outer bound.

Install the checked files on Amarillo without starting the timer during a GPU
embargo:

```bash
cd /home/jalsarraf/git/klukai
install -d -m 0755 /home/jalsarraf/.local/bin \
  /home/jalsarraf/.config/systemd/user
install -m 0755 \
  ops/amarillo/scripts/klukai-memory-seed-day-condition.py \
  /home/jalsarraf/.local/bin/klukai-memory-seed-day-condition
install -m 0644 \
  ops/amarillo/systemd/klukai-memory-archive-seed.service \
  ops/amarillo/systemd/klukai-memory-archive-seed.timer \
  /home/jalsarraf/.config/systemd/user/
systemctl --user daemon-reload
systemd-analyze --user verify \
  /home/jalsarraf/.config/systemd/user/klukai-memory-archive-seed.service \
  /home/jalsarraf/.config/systemd/user/klukai-memory-archive-seed.timer
systemctl --user is-active klukai-memory-archive-seed.timer && exit 1 || true
```

The last command is a required embargo check. After GPU acceptance is complete,
enable the schedule explicitly:

```bash
systemctl --user enable --now klukai-memory-archive-seed.timer
systemctl --user list-timers klukai-memory-archive-seed.timer
```

Do not manually start the service as a test: on an eligible date that would run
the real archive pipeline and spend account renders (or, in the legacy
`comfyui` mode, acquire GPU leases). Static verification is:

```bash
python3 -m unittest -v ops/amarillo/tests/test_memory_seed_schedule.py
systemd-analyze calendar '*-*-* 04:00:00 America/Chicago'
```

For a real scheduled run, inspect only metadata and journal output; never print
the root `.env` or its rotated tokens:

```bash
systemctl --user status klukai-memory-archive-seed.timer --no-pager
journalctl --user -u klukai-memory-archive-seed.service --since today --no-pager
```
