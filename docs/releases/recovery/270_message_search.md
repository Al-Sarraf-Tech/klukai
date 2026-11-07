# Migration 270: conversation search recovery plan

## Data preserved

Migration `270_message_search.sql` installs PostgreSQL's bundled `pg_trgm`
extension if absent and adds a GIN index over original message content. It
does not delete, rewrite or summarize any message, memory, annotation,
episode, exchange, user or affection row. It does not change model recall.
The extension lives in `public` and the index explicitly qualifies its
operator class, including when the application tables use another schema.

Before rollout retain aggregate counts for `users`, `companion_users`, `companion_messages`,
`companion_memories`, `companion_episodes`, `companion_exchanges` and
`companion_affection`, plus the affection score total. Use the established
private runtime wrapper; never print source rows or credentials. Counts
must not decrease. Affection can change during live conversations, so a
changed live total must be accounted for, not silently treated as loss or
asserted equal. The disposable migration proof compares both counts and
contents exactly with writers stopped.

The normal release engine must take and restore-verify its fresh dump under
`/mnt/nvmeINT/backups/klukai/releases/` before recreating core. Retain the
receipt and previous image ID. Standard transactional index creation can
hold a write lock while it builds; the migration runner bounds lock waiting.
The synthetic build time is not a prediction of production startup time.
Do not weaken the existing ready/hold/5xx gates to accommodate a slow build.

## Rollback

The old image alone cannot start after migration 270 commits: its migration
directory lacks a file recorded in the database ledger. The old application
code is otherwise compatible with the additive index. Preserve the exact
forward migration files and ledger; do not drop the extension/index, delete
a ledger entry, or restore an older database.

Pause deploy-on-merge using its normal `pause` command and confirm no
deployment is inflight. Validate the exact committed same-stem recovery
document with `recovery_plans(changed_paths, candidate_sha, repo)` and retain the independent
review/proof. Do not alter the automatic deployer's approval policy.

Before the certified rollout, acquire the shared deployment lock once for
the complete reviewed manual transaction:

1. Capture the running image ID and a private copy of the non-secret
   `~/.local/state/klukai/release/core-image.json`. Preserve all existing
   runtime overrides. Do not inspect the encrypted environment or any login
   credential file.
2. Export the candidate commit's complete `docker/core/migrations/` tree to
   `~/.local/state/klukai/release/migration-snapshots/<candidate-sha>/`.
   Use committed bytes (`git archive <candidate-sha> docker/core/migrations`),
   not a mutable worktree copy. Record a SHA-256 manifest, verify it against
   the commit, and make the snapshot read-only. The snapshot must remain
   outside the deployer-managed checkout and be readable by core's UID.
3. Add a read-only bind of that snapshot's `docker/core/migrations` to
   `/app/migrations` in the existing pin's
   `services.companion-core.volumes`, replacing only any previous mount of
   that target. Preserve every other pin field and mount. Write atomically.
   Do not recreate core yet. This mount must be in place before the release
   engine records its recovery baseline. `Target.write_pin` preserves the
   mount when switching either to the candidate or back to the previous
   image, and the snapshot survives checkout restoration.
4. Invoke the existing pinned `Deployer.produce` transaction with
   `accept=True` and `plans=validated_recovery_plans` after its normal
   candidate/main, certification, source, artifacts and recovery preflight.
   This is the existing manual `--accept-manual-rollback` operator acceptance
   expressed through the coordinated core/web transaction. Hold the shared
   lock once around this call; do not invoke the standalone CLI from inside
   the lock, which would try to acquire it again. Retain the existing signature, exact-source,
   backup/restore, readiness, hold, 5xx and coordinated core/web rollback
   checks. This flag does not authorize changing branch protection or the
   automatic deployer's approved-PR policy.

After the complete forward rollout, web switch and live checks succeed,
verify the candidate image's bundled migrations match the snapshot. Remove
only the temporary `/app/migrations` bind from the pin's future compose
configuration (no recreate needed), preserving every other override. The
running candidate may retain its identical bind until the next recreate;
future images must use their own migration directory so a stale snapshot
cannot hide a later migration. Retain the snapshot and recovery evidence.

If a later manual rollback is necessary, pause deploy-on-merge with its
`pause` command. Reinstate the exact snapshot mount while preserving every
runtime override in the current pin, change only the image to the release receipt's immutable
rollback tag, restore the previous atomic web release, and recreate core:

```sh
python3 ops/amarillo/scripts/klukai_compose.py up -d --no-build --no-deps companion-core
```

Keep the snapshot mount while the old image is selected; removing it would
break its next restart. A future release after rollback must explicitly
replace/remove that bind only after checking that its bundled directory
contains every applied migration. Verify normal preflight before resuming
automation.

If migration 270 itself fails, its transaction leaves no applied-270 ledger
entry. Automatic rollback with the forward snapshot will retry the same
migration and can fail again if the cause persists. This is a material limit,
not a guaranteed rollback. Inspect only migration-ledger metadata through
the runtime wrapper: if 270 is absent and every prior migration/hash matches
the previous image, remove the temporary bind and recreate the previous
image using its original migrations. If 270 is present, retain the forward
snapshot. Never remove a committed ledger entry or alter its recorded hash.
Do not retry index creation indefinitely against a persistent lock or
privilege failure; recover the previous app as above and investigate.

No database restore is required, so all writes since deployment survive.
If physical database recovery is separately necessary, stop writers, take
a new dump, and reconcile post-release writes before considering a restore.
Never overwrite live history with the pre-release dump.

## Verification

Before production, on disposable PostgreSQL 17 with synthetic rows:

- Apply the prior migrations, seed each protected table, record exact rows,
  counts and affection totals; apply the complete candidate migrations and
  verify all source data unchanged. Confirm the new index is valid and the
  extension/operator class resolve under a non-public test search path.
- Execute the actual previous core image's migration runner against the
  same disposable database with the exact candidate migration snapshot
  mounted read-only at `/app/migrations`. It must accept every ledger hash
  and return successfully. Run only the migration entry point, with no
  application workers, inference credentials or production network.
- Recheck synthetic rows and migration hashes after the old runner. Keep
  the image ID, snapshot digest and command/result evidence with the release.

After forward rollout, verify the candidate image ID and web revision,
`/health` and `/api/health/ready`, existing aggregate counts, and the normal
`scripts/e2e_live.py` checks `health,auth-required,bad-token,authed-read` as
the dedicated `claude` test user. Use synthetic test-user history only for
search/source checks; do not inspect the owner's conversations. Confirm
both new routes reject missing authentication, searches return no-store,
and the source context belongs to the authenticated user/conversation.

After rollback, verify the previous immutable image, its healthy/ready
state, the preserved snapshot mount and matching migration ledger, previous
atomic web release, aggregate data checks and the same read-only live checks.
No test or rollback may send a chat message, generate an image, or alter
Fish voice/provider settings.

### Recorded disposable rehearsal

The November 7 rehearsal passed against the actual previous core image,
PostgreSQL 17.9, a non-superuser database-owner role and a non-public search
path. Migration 270 applied over 10,001 synthetic messages in 0.228 seconds;
all 30 protected-table counts/content hashes and affection total were
unchanged. A simulated post-release message and affection update also
survived recovery exactly. The unmounted old image refused the missing
migration as expected; with the exact read-only snapshot it accepted all
28 recorded hashes and replayed no SQL, including a repeated run.

The [rehearsal record](../../research/2026-11-07-conversation-search-rollback.json)
binds image and migration digests. All rehearsal containers were removed.
This proves migration-loader compatibility and synthetic data preservation,
not full application startup or production timing. Require the final
certified migration tree to match this record before rollout.
