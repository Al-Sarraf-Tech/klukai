# Runbook Index

Top-10 alerts klukai's on-call (i.e., jalsarraf) might encounter.
Each runbook follows the same structure: Symptom → Immediate action
(< 5-10 min) → Root cause → Verification → Post-incident.

## Severity ladder

- **P1** — production-down or user-data-at-risk. Wake someone immediately.
- **P2** — degraded; some users affected, no data loss yet. Same-day fix.
- **P3** — slow / annoying; can wait for the next business day.
- **P4** — informational; no action required, just visible in dashboards.

## Runbook list

| # | Runbook | Severity | Trigger |
|---|---|---|---|
| 1 | [db-down](db-down.md) | P1 | `/health` `database: down` |
| 2 | [redis-down](redis-down.md) | P2 | `/health` `redis: down` |
| 3 | [qdrant-down](qdrant-down.md) | P2 | `/health` `qdrant: down` |
| 4 | [lm-studio-cold](lm-studio-cold.md) | P3 | Chat first-token >15s |
| 5 | [voice-unreachable](voice-unreachable.md) | P2 | `/health` `voice: down` |
| 6 | [comfyui-down](comfyui-down.md) | P3 | Image gen 5xx |
| 7 | [high-latency](high-latency.md) | P2 | SLO burn-rate alert fired |
| 8 | [disk-space](disk-space.md) | P2 | `/mnt/nvmeINT` > 90% |
| 9 | [memory-leak](memory-leak.md) | P2 | RSS climbing > 50%/24h |
| 10 | [auth-fail-spike](auth-fail-spike.md) | P2 | Auth fail rate > 5% |

## How runbooks evolve

- A runbook is created from a real incident or a likely-incident
  imagined during planning.
- Each P1/P2 incident MUST result in either: (a) a new runbook, or
  (b) an update to an existing one.
- Phase 3+: alert annotations include `runbook_url` linking directly
  to the relevant anchor here.
- Phase 5: a quarterly drill picks 3 runbooks at random and walks
  them top-to-bottom on a fresh terminal. Drift is filed as PRs.

## Cross-cutting absolute rules

Per global CLAUDE.md, NEVER do these regardless of what a runbook
suggests:

1. **NEVER delete chat memories** (`feedback_never_delete_chat.md`)
   — chat, episodes, affection, Qdrant vectors are sacred. Recovery
   = restore from backup, never recreate.

2. **NEVER rotate passwords autonomously**
   (`feedback_no_password_changes.md`) — escalate to user.

3. **NEVER bypass HTTPS gateway** — internal services bind 127.0.0.1
   per Phase 1 hardening; Cloudflare + nginx are the only public
   ingress.

4. **NEVER skip orchestrator pre-push scan** — required before any
   git push.

If a runbook step appears to violate one of these, **stop, escalate**.

## Where this lives operationally

When alerts fire (Phase 2+), Grafana includes the runbook_url in the
notification. Operator clicks → lands on the right runbook anchor →
follows the steps. Self-contained on-call experience.
