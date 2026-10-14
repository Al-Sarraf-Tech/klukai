# Amarillo operations

Amarillo owns Klukai's core services, persistent state, PWA, event bridge, and
the native [Intel Arc XTTS release](voice_xpu/README.md). In the current
deployment, Sonnet 5.5 with medium adaptive thinking is the interactive primary;
Dominus is the local secondary. Arc handles English speech independently of
that choice. Local pictures and live portraits still use Dominus ComfyUI, and
transcription keeps its separate Dominus endpoint.

Klukai **v1.1.0** prepares opt-in OpenAI scene images and Anthropic dream/Her POV
writing. Checked-in defaults remain `comfyui` and `local`, with zero daily and
monthly creative attempt allowances. After publication, the owner authorized
the existing Claude key for creative writing: production now sets the text
provider to `anthropic`, with both daily and monthly attempt limits at `12`.
This permits 12 total monthly attempts shared across all users and creative
text paths. Pictures remain local ComfyUI, and cloud image limits remain zero.
Consult the
[provider handoff](../../docs/handoff/2026-10-14-dream-image-preparation.md) before
changing them; the [local GPU configuration](../../README.md#use-local-gpus)
documents how to keep inference local.

Operator changes must preserve the independent hosts' admission rules: no
synthetic warmup when local load-on-demand is selected, bounded GPU residency,
Dominus gaming guards, and Arc's thermal/process limits. A core or PWA release
does not authorize starting a GPU service. Read the
[Dominus boot/publish handoff](../../docs/handoff/2026-09-25-dominus-tailscale-publish.md)
before repair or startup work.

## Memory archive seeding

Amarillo owns the memory-seeding schedule because `companion-core`, its
database, and the seed script live there. The job still reaches LLM and ComfyUI
through Klukai's authenticated Tailscale clients and the bounded Dominus GPU
lease; the user unit never connects to a LAN or raw ComfyUI port.

Historical seeding is deliberately local-only. If either
`KLUKAI_IMAGE_PROVIDER` or `KLUKAI_CREATIVE_TEXT_PROVIDER` selects a cloud
provider, the seed script exits before reading history, acquiring a GPU lease,
or dispatching an API call. Existing local watermarks cannot safely deduplicate
paid partial batches; durable source-aware backfill is deferred. Do not work
around this refusal by increasing attempt limits or deleting the ledger.

The timer checks every day at 04:00 America/Chicago. Its `ExecCondition` runs
only on even Unix-epoch calendar days, producing the required deterministic
every-other-local-day cadence. The same condition rejects execution outside
03:00–06:00, including a manual or delayed invocation. `Persistent=false`
prevents boot catch-up, and the service timeout ends a stuck run before 06:00.

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
the real archive pipeline and acquire GPU leases. Static verification is:

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
