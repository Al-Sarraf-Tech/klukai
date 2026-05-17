# ADR-0014: Off-site backup — amarillo → dominus nightly tar

- **Date:** 2026-04 (formalized 2026-05-16)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

klukai's three memory backends (PG, Qdrant, Redis snapshot + the
`companion-images` volume) live on amarillo. A single-host failure
(drive death, power surge, accidental `docker volume rm`) would
delete all chat history and memories — catastrophic per global CLAUDE.md
`feedback_never_delete_chat.md`. amarillo also has no off-host RAID.

## Decision

Nightly backup chain:

1. `scripts/backup-companions.sh` on amarillo dumps PG +
   exports Qdrant snapshots + tars `companion-images` →
   `/mnt/nvmeINT/backups/`.
2. ~30 min later, `scripts/offsite-backup.sh` tars
   `/mnt/nvmeINT/backups/` and `scp`s to
   `dominus:/c/Users/jalsarraf/klukai-backups/backups-YYYYMMDD-HHMM.tar.gz`.
3. 30-day retention on dominus (older tars auto-pruned by filename
   date).

Caught a silent failure on 2026-04-20: `backup-companions.sh` had been
broken for 5 days due to stale container references
(`kairi-postgres-1`, `aichat-aichat-db-1`). Per
`feedback_offsite_backup.md`, this exact failure pattern is why the
script now verifies dump non-emptiness and the verify-backups timer
checks dump size + gzip validity.

Tested-restore drill: `scripts/restore-from-backup.sh` runs monthly
via systemd timer (Phase 2 deliverable, this session). Pulls latest
tar, restores to scratch PG, verifies tables + row counts. Pass =
backup is real; fail = pages.

## Consequences

- **dominus is the recovery point**. If dominus also dies, recovery
  from earlier-than-30d backups is impossible.
- **No cloud backup** — privacy + cost. Tail-risk of
  amarillo + dominus simultaneous loss is accepted.
- **Backup never touches prod data** — pure read-only export +
  off-host write.
- **Tar over rsync**: dominus runs Windows Git Bash with no rsync
  in PATH. Backups are few MB; full transfer each night is fine.
- **Recovery time**: ~10-15 min for full restore (tar pull + PG
  import + Qdrant restore). RTO acceptable for a personal product.

## Alternatives considered

- **S3 / Backblaze cloud backup**: privacy concern + cost +
  encryption complexity. Phase 4 could revisit with client-side
  encryption.
- **rsync from amarillo**: dominus lacks rsync (Git Bash). Could
  install but tar is simpler.
- **Hourly backups**: PG dumps every hour adds load + storage. Daily
  is sufficient given RPO tolerance.

## Related

- `scripts/backup-companions.sh` (amarillo)
- `scripts/offsite-backup.sh` (amarillo → dominus)
- `scripts/verify-backups.sh` (sanity)
- `scripts/restore-from-backup.sh` (monthly drill)
- `feedback_offsite_backup.md` (global CLAUDE.md)
- `feedback_never_delete_chat.md` (data is sacred)
- `docs/runbooks/db-down.md` (recovery procedure)
- ADR-0003 (three-tier memory — all three backed up)
