# Runbook: Disk Space Low

**Severity:** P2 (writes failing or imminent)
**SLO breach:** All write endpoints (audit, memory, image save) fail.

## Symptom

- `df -h /mnt/nvmeINT` shows >90% used
- companion-core logs: `disk full` errors
- PG WAL writes failing
- Image generation fails on save
- Backup jobs fail with `No space left on device`

## Immediate action (< 5 min)

1. Check disk usage:
   ```bash
   df -h /mnt/nvmeINT
   ```
2. Find the biggest consumers:
   ```bash
   du -h /mnt/nvmeINT/* 2>/dev/null | sort -hr | head -20
   ```
3. Common culprits:
   - `/mnt/nvmeINT/logs/` — old log files
   - `/mnt/nvmeINT/backups/` — old backup tars
   - `/mnt/nvmeINT/data/postgres/` — DB growth or WAL
   - Container volumes (Qdrant snapshots, Docker overlay)
4. Free space (in this order):
   - Rotate logs: `logrotate -f /etc/logrotate.d/klukai`
   - Prune old backups: `find /mnt/nvmeINT/backups -mtime +30 -delete`
   - Docker prune: `docker system prune -f` (NOT `-a` — keeps images)
   - PG vacuum: `docker exec infra-postgres psql -U aichat -c "VACUUM FULL;"`
     **WARNING:** locks tables; schedule during low traffic.

## Root-cause investigation

| Pattern | Likely cause | Long-term fix |
|---|---|---|
| Logs >5GB | Log rotation not working | Fix logrotate config; alert on log size |
| Backups >100GB | Retention too long | Tighten `find -mtime` window |
| PG >50GB suddenly | Table bloat | Schedule regular VACUUM; check autovacuum |
| Qdrant snapshots growing | Snapshot retention | Set Qdrant snapshot retention env |
| Many small files | Test artifacts not cleaned | Add cleanup to test fixtures |

## Verification after cleanup

1. `df -h /mnt/nvmeINT` shows <80% used.
2. Test a write endpoint (chat turn) — confirms PG writes work.
3. Run `scripts/verify-backups.sh` to confirm backups still passing.

## CRITICAL: Don't break klukai's memory

Per global CLAUDE.md `feedback_never_delete_chat.md`: chat messages,
episodes, affection, Qdrant vectors are SACRED. Disk-pressure cleanup
must NEVER touch:

- `/mnt/nvmeINT/data/postgres/` content (the DB itself)
- `/mnt/nvmeINT/data/qdrant/` (vector storage)
- `/var/lib/docker/volumes/companion-images/` (memory archive images)

If you must reclaim from these, **restore from offsite backup first**.

## Long-term mitigation

- Add disk alert at 80% (not 90% — gives lead time)
- Auto-trim old logs nightly via systemd timer
- Auto-prune backups via timer (already exists; verify it's running)
- Capacity plan: track growth rate, predict full date

## Related

- `feedback_never_delete_chat.md`
- `scripts/offsite-backup.sh` (offsite copies safe regardless)
- `scripts/verify-backups.sh`
