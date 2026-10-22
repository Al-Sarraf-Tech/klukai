# Release recovery plans

Automatic rollback restores only the core image. It cannot undo a database
migration: core applies migrations at startup and refuses to start when an
applied migration file is missing. It also cannot undo a `docker-compose.yml`
change, because a rollback recreates core with the new compose file. Chat
history and user data are sacred, so deploy-on-merge never ships either kind of
change unless a reviewed recovery plan comes with it.

A plan is reviewed because it is part of the approved PR that adds the change,
and it lands in the same merge. The deployer checks both. It needs the plan,
and it needs the release commit to be the merge of a PR approved on its final
head by an allowed reviewer. A direct push carrying a migration and its own
plan is never deployed automatically. The deployer
(`ops/amarillo/scripts/klukai-deploy-on-merge.py`, through
`klukai-release.py recovery_plans`) checks the plan in the release commit
itself. If a plan is missing or incomplete, the commit is held and the owner is
told. Nothing is deployed.

## Naming

| Change | Plan file |
| --- | --- |
| `docker/core/migrations/<name>.sql` (added, changed or removed) | `docs/releases/recovery/<name>.md` |
| `docker-compose.yml` | `docs/releases/recovery/compose-<blob12>.md` |

`<blob12>` is the first 12 hex characters of the new compose file's git blob
ID: `git hash-object docker-compose.yml | cut -c1-12`. Naming the plan after the
file's content means a later compose change needs its own plan.

## Required sections

A plan is accepted only with these three headings, written for whoever is on
call when the release goes wrong:

```markdown
# <migration or compose change>: recovery plan

## Data preserved
What the change does to existing rows (chat messages, episodes, memories,
affection, users). It must never delete or rewrite them. State the
aggregate checks (row counts, score totals) that prove nothing was lost.

## Rollback
Exact steps to get back to the previous working release. For a migration:
whether the previous image still starts (usually not), the release dump to
restore from (`/mnt/nvmeINT/backups/klukai/releases/`, taken and restore-
verified by the deployer before rollout), and how to restore it without losing
writes made since. For compose: the previous file and the recreate command.

## Verification
How to confirm the forward release and, if used, the rollback: queries,
`scripts/e2e_live.py` checks as the `claude` test user, health endpoints.
```

Never put credentials, private rows or chat content in a plan. The repository
is public.
