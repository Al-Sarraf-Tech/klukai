# Migration 260: recovery plan

## Data preserved

This adds generated search columns to existing episode and exchange tables. It does not delete or rewrite messages, annotations, episodes,
exchanges, users or affection. The one-time Qdrant backfill is a separate,
insert-only transaction; existing PostgreSQL rows and Qdrant points remain intact.

Before release retain aggregate row counts for users, messages, memories,
episodes, exchanges and affection, and the affection score total. The release
manager takes and restore-verifies a dump under
`/mnt/nvmeINT/backups/klukai/releases/`. Preserve its receipt and a private copy
of the previous `~/.local/state/klukai/release/core-image.json` as
`~/.local/state/klukai/release/pre-cloud-only.json`. Compare aggregates after
startup and again after the additive backfill; counts must never decrease.

## Rollback

Pause deploy-on-merge with its `pause` command. Keep the forward checkout and
the exact migration directory: the previous image refuses to start if applied
migration 260 is absent. Do not delete the ledger entry or downgrade the database.

Restore the saved pre-cloud-only image pin, adding a read-only bind mount of
`/home/jalsarraf/git/klukai/docker/core/migrations` at `/app/migrations` to that
pin's `services.companion-core.volumes`. The older code ignores the additional
search columns. Set its `LM_STUDIO_URL`, `INFERENCE_URL`, `VOICE_URL`,
`VOICE_STT_URL` and `COMFYUI_URL` environment overrides to
`http://127.0.0.1:9`; retain `VOICE_BACKEND=fish` and
`KLUKAI_IMAGE_PROVIDER=agy`. Thus rollback does not reconnect local inference.
Recreate only core with:

```
python3 ops/amarillo/scripts/klukai_compose.py up -d --no-build --no-deps companion-core
```

No dump restore is needed for this additive migration, so writes since release
survive. If physical database recovery is separately necessary, stop writers,
take a new dump first and reconcile post-release writes before restoring a
backup. Never overwrite the live database with the older release dump.

## Verification

Check healthy/readiness endpoints, original row counts and affection totals.
Run `scripts/e2e_live.py` with the dedicated `claude` test identity. On forward
release verify PostgreSQL memory recall and the cloud-only invariant; on
rollback verify the previous image starts with the exact applied migration
bytes and the disconnected inference overrides. The disposable PostgreSQL
test `test_cloud_only_migration_preserves_annotations_and_history` verifies
that migration 260 preserves existing annotation history.
