# ADR-0013: klukai and kairi are separate characters with separate data

- **Date:** 2026-04 (formalized 2026-05-16)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

The amarillo host runs two companion AI stacks simultaneously:
- **klukai** (the primary, GFL2 Klukai character)
- **kairi** (a separate companion AI, different character)

They share infrastructure (PG, Redis, Qdrant containers) for
efficiency but their data is logically separate. Confusion has
happened — per `feedback_klukai_kairi_separate.md`, `kairi_memory`
is NOT orphaned data from klukai.

## Decision

**ABSOLUTE rule**: klukai and kairi are separate characters with
separate data. Specifically:

- Database tables: klukai uses `companion_*` prefix; kairi uses
  `kairi_*` prefix. Never cross-query.
- Qdrant collections: klukai uses `episodic_memories`,
  `relationship_facts`; kairi has its own collection set.
- Redis keys: klukai uses `session:*`, `mood:*`; kairi has its own.
- Compose project: pinned via `-p companion` for klukai, `-p kairi`
  for kairi, so container names don't collide.
- Backup scripts: separate paths under `/mnt/nvmeINT/backups/{klukai,kairi}/`.

Per the 2026-04-20 rename commit (`35a6c65 refactor: rename companion
-> klukai path references`), klukai's path references in code were
updated to use the `klukai` name, but the DB tables, container names,
volume names, and Redis key prefixes were preserved as
`companion_*` to avoid data migration. This is intentional.

## Consequences

- **No accidental cross-character data leaks**: queries scope to
  prefix automatically.
- **Backups are separate**: restore from klukai backup won't
  overwrite kairi data and vice versa.
- **Audit scripts** (`scripts/audit-memories.sh`) check only klukai
  tables. Future kairi audit would be a separate script.
- **Rename without migration**: the `companion_` table prefix is
  a historical artifact preserved for stability. Renaming tables
  would require data migration with risk; the path-level rename
  was the right scope.
- **Documentation**: every script + ADR refers to klukai by name
  except where touching the legacy `companion_*` table names.

## Alternatives considered

- **Single shared character schema**: rejected — characters have
  distinct personalities, distinct affection states per user,
  distinct memory archives. Cross-character pollution would
  corrupt both.
- **Migrate tables to `klukai_*`**: out of scope; data migration
  risk > stability gain. Preserved per `feedback_rename_preserves_identity.md`.

## Related

- `feedback_klukai_kairi_separate.md` (global CLAUDE.md, ABSOLUTE)
- `feedback_rename_preserves_identity.md`
- Commit `35a6c65 refactor: rename companion -> klukai path references`
- `scripts/backup-companions.sh` (separates klukai + kairi dumps)
- `scripts/audit-memories.sh` (klukai-scoped)
