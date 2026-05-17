# ADR-0003: Three-tier memory — Redis → Qdrant → PostgreSQL

- **Date:** Origin (formalized 2026-05-16)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

Klukai's character is the product. Her sense of *continuity* —
remembering past conversations, knowing Commander's preferences,
recalling specific moments at the right time — is what makes
interactions feel personal vs scripted. This requires three
different memory access patterns:

- **Session state** (current mood, turn count, last bot message):
  read every turn, written every turn, ms-latency budget.
- **Semantic recall** ("remember when we talked about X?"): vector
  search across episodes, ~100s of ms acceptable.
- **Factual record** (audit log, affection state, message archive,
  user accounts): durable storage, indexed lookups, ~ms latency.

A single store can't do all three well: PG is too slow for hot
session reads, Redis can't do semantic similarity, Qdrant isn't
designed for transactional writes.

## Decision

Use three backends, each scoped to one pattern:

| Tier | Backend | Use cases | Persistence |
|---|---|---|---|
| Session | Redis | mood, turn buffer, rate-limit buckets, push subscriptions | Volatile (RDB checkpoint OK) |
| Vector | Qdrant | episodic memories, recalled exchanges, relationship facts (embedded) | Durable, snapshotted |
| Factual | PostgreSQL | messages, memory_archive (kept), affection_state, users, audit_log | Durable, WAL-replicated, backed up |

## Consequences

- **Three failure modes** instead of one. Each gets a runbook:
  `redis-down.md`, `qdrant-down.md`, `db-down.md`.
- **Chat memory is SACRED** (per global CLAUDE.md
  `feedback_never_delete_chat.md`): PG companion_messages and Qdrant
  vectors are never deleted as part of cleanup. Recovery = restore
  from backup.
- **Graceful degradation matrix** (Phase 4 work):
  - PG down → chat fails (can't persist new turns) — P1
  - Redis down → users logged out, rate limit falls open — P2
  - Qdrant down → chat continues without recall context — P2
- **Memory integrity audit** (`scripts/audit-memories.sh`) crosses all
  three to detect data loss.

## Alternatives considered

- **PG-only with `pgvector`**: simpler, but Qdrant's HNSW index +
  payload filtering is purpose-built and faster at scale.
- **Redis-only with vector module**: Redis vector module is newer
  and less battle-tested for our query patterns.
- **DynamoDB / firestore (cloud)**: introduces network dependency
  + vendor lock + cost. Single-host LAN is faster and free.

## Related

- `app/memory.py` — three-tier memory manager
- `app/db.py` — PG pool wrapper
- `docs/runbooks/{db,redis,qdrant}-down.md`
- `scripts/audit-memories.sh`
- `feedback_never_delete_chat.md` (global CLAUDE.md)
- ADR-0014 (off-site backup covers all three)
