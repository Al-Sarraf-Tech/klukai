# ADR-0014: Off-host recovery copy over Tailscale

- **Date:** 2026-04; amended for Nobara on 2026-08-01
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

Klukai's durable memory is split across PostgreSQL, Qdrant, and the
`companion-images` volume on `amarillo`. Losing that host or its local backup
filesystem must not also destroy the recovery copy. The former destination was
the lost Windows installation on `dominus` and depended on its user-profile
drive mapping; that path and runtime no longer exist.

`dominus-nobara` has a fast NVMe RAID 0 mounted at `/mnt/nvmer0`. RAID 0 adds
capacity and throughput but provides no redundancy: loss of either member
loses the array. A copy stored there is off-host protection for data whose
primary is on Amarillo. It is not an independent backup of any data whose
primary is already on `dominus-nobara`, and it does not protect against loss of
both hosts.

## Decision

The Amarillo backup producer deposits Klukai and Kairi database dumps, Qdrant
snapshots, and companion images below `/mnt/nvmeINT/backups`. The repository's
`scripts/offsite-backup.sh` archives only the recovery roots `klukai`, `kairi`,
and `qdrant` (not unrelated application backups) into an immutable dated
archive at:

```text
/mnt/nvmer0/services/ai-stack/backups/amarillo/klukai/
  backups-YYYYMMDD-HHMMSS.tar.gz
  backups-YYYYMMDD-HHMMSS.tar.gz.sha256
```

The following are mandatory invariants:

1. `/mnt/nvmeINT` must be the live Amarillo mount before the script reads a
   source, creates a log, or creates temporary data.
2. `/mnt/nvmer0` must be the live target mount before the remote script creates
   or reads a backup path. A missing RAID fails closed rather than creating a
   root-filesystem fallback.
3. SSH alias `dominus-nobara` must resolve to the locked Tailscale IPv4
   `100.107.121.5`. The remote process verifies that `SSH_CONNECTION` is
   `100.111.198.19` → `100.107.121.5`; LAN routing is not a fallback.
4. The destination is a dedicated, mode-0700 directory with an ownership
   marker. A non-empty unmarked directory is never adopted automatically.
5. At least one recent, gzip-valid database dump must exist before transfer.
6. Each archive is streamed to a unique partial name and validated remotely.
   Its SHA-256 sidecar is installed first; the validated archive is then
   atomically renamed into visibility. A completed archive is never
   overwritten.
7. Automated retention is report-only. The script identifies archives older
   than the retention objective but does not delete them. Pruning requires an
   explicit operator action after another independent, non-RAID copy is
   verified.

The nominal objective remains 30 daily recovery points, but capacity policy
must never silently outrank preservation of chat history and memories.

## Recovery and verification

`scripts/restore-from-backup.sh` is the routine drill. It validates the same
Tailnet and mount invariants, copies the newest archive plus its checksum into
guarded scratch space on `/mnt/nvmeINT`, rejects unsafe tar paths, and restores
PostgreSQL only into a uniquely named scratch container. It never connects to
production PostgreSQL.

`scripts/disaster-recovery.sh` defaults to a read-only plan. Live recovery
requires `--execute` plus two explicit confirmation values acknowledging the
shared `aichat` database. It verifies the archive, checksum, PostgreSQL dump,
Qdrant snapshots, and image payload before stopping only the Amarillo
`companion-core` service. It never manipulates the canonical GPU Compose stack
on `dominus-nobara`.

The Nobara model releases, service images, and unique GPU-side assets follow
the separate backup/rollback gates in `ops/dominus-nobara/RUNBOOK.md`. A copy
within `/mnt/nvmer0/services/ai-stack/backups` is not sufficient protection
for another path on the same RAID.

## Consequences

- Amarillo has an authenticated, checksummed, off-host recovery point reachable
  only through Tailscale SSH.
- A missing source or destination mount stops the workflow before it can write
  into an identically named directory on a root filesystem.
- Completed archives and legacy recovery data are preserved by default.
- The target remains a single RAID 0 failure domain. A third copy on an
  independent device or encrypted remote store is required for protection
  against target-array loss or simultaneous host loss.
- Cold disaster recovery remains operator-gated because PostgreSQL and Qdrant
  restoration is intentionally destructive.

## Alternatives considered

- **Treat the target RAID as the backup:** rejected. A RAID 0 array has no
  redundancy and cannot back up data stored on itself.
- **Private-LAN SSH fallback:** rejected. It weakens the fixed Tailnet trust
  boundary and can silently route a transfer to the wrong interface.
- **Automatic age-based deletion:** rejected. Age alone does not prove another
  recoverable copy exists.
- **Object storage:** still viable if encrypted client-side; it would provide
  the independent failure domain that the two-host copy lacks.

## Related

- `scripts/offsite-backup.sh`
- `scripts/verify-backups.sh`
- `scripts/restore-from-backup.sh`
- `scripts/disaster-recovery.sh`
- `ops/dominus-nobara/RUNBOOK.md`
- `docs/runbooks/db-down.md`
- ADR-0003 (three-tier memory)
- ADR-0015 (Windows/WSL2 is not a deployment target)
