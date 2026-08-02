# Amarillo scheduled operations

## Memory archive seeding

Amarillo owns the memory-seeding schedule because `companion-core`, its
database, and the seed script live there. The job still reaches LLM and ComfyUI
through Klukai's authenticated Tailscale clients and the bounded Dominus GPU
lease; the user unit never connects to a LAN or raw ComfyUI port.

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
