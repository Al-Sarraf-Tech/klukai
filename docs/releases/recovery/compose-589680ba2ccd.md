# Cloud-only compose: recovery plan

## Data preserved

Only the core configuration changes: obsolete local model, embedding, voice,
and renderer settings are removed. Networks and persistent mounts remain.
No database or application data is removed. Retain the release backup receipt,
aggregate row counts and affection total, and the previous image pin at
`~/.local/state/klukai/release/pre-cloud-only.json`.

## Rollback

Pause deploy-on-merge before manual recovery. Restore that previous image pin
and follow [migration 260's recovery plan](260_memory_full_text_search.md),
including its read-only migration mount and disconnected local inference
settings. The previous compose file is available at commit `0c10ee5`.
Its effective cloud voice/image settings are retained in the saved pin;
restore them through the existing `klukai_compose.py` override mechanism,
without printing or copying the encrypted private environment.

Recreate only `companion-core` with
`python3 ops/amarillo/scripts/klukai_compose.py up -d --no-build --no-deps companion-core`.
Leave the forward checkout and current migration files in place, preserve all
volumes and keep the application on Amarillo. Do not restore the retired
Dominus stack or enable the local embedding endpoint.

## Verification

Confirm the pinned image ID, core health/readiness, unchanged database counts
and affection total, the authenticated voice capabilities and the existing
`claude` test-user smoke checks. Confirm no effective URL points at Dominus
and that the image worker still uses its existing cloud provider and limits.
