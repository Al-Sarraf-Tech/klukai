# Klukai Architecture

> Companion PWA + FastAPI core on a two-host private mesh. Provider configuration below reflects the owner's October 14, 2026 deployment. Reliability work on the elite-refactor branch is tracked separately in `handoff/2026-10-14-elite-refactor-progress.md`; planned behavior is not deployment evidence. Per-decision history lives in `docs/adr/`.

**Redaction:** mesh IPs, MagicDNS names, and secret values are omitted. Use host roles **core host** and **GPU host**.

## Topology

```
Existing authorized cloud services
  Sonnet interactive + capped creative text · Brave search
  Signed-in Agy/Grok account image worker (core host, private Unix socket)
      ▲
      │
Public TLS edge
      │
      ▼
┌─────────────────────────────┐         private mesh          ┌─────────────────────────────┐
│         CORE HOST           │ ─────────────────────────────►│         GPU HOST            │
│  gateway · companion-core   │                               │  lmstudio-compat (ingress)  │
│  Postgres · Redis · Qdrant  │                               │  llama-router (internal)    │
│  events-bridge · RabbitMQ*  │                               │  ComfyUI (leased only)      │
│  observability stack        │                               │  voice · STT                │
└─────────────────────────────┘                               └─────────────────────────────┘
```

\* Broker may share the core host or an adjacent ops stack; routing keys are host-scoped (`host.<name>.…`).

The GPU stack retains ComfyUI for explicit rollback/other authorized work. Production Klukai image requests use the account worker; there is no automatic GPU image fallback.

### Trust boundaries

| Boundary | Policy |
|---|---|
| Internet → gateway | TLS at the edge; static `/app/` public; API authed |
| Core → GPU | Private mesh only; bearer / lease tokens |
| Core → account image worker | Private Unix socket, scoped authentication, owner allowlist, pinned reference paths owned by the worker; no conversational history in account prompts |
| Bridge → RabbitMQ | Credentials in env; not committed |
| Process crash | Job/defer rows in Postgres survive; queue redelivery is subject to idempotent claims and the no-reclaim rule for ambiguous cloud work |

## companion-core

Single FastAPI process. Important modules:

| Module | Role |
|---|---|
| `chat.py` / `chat_handlers.py` | Authenticated multi-device WebSocket conversation pipeline |
| `llm_router.py` | Authorized Sonnet interactive primary; Dominus local secondary |
| `memory_her_pov.py` | Her POV pipeline + durable job board |
| `deferred.py` | One-shot future tasks (Postgres + bridge timer) |
| `proactive/` | Cron engine + durability catch-up |
| `memory*.py` | Three-tier memory + archive |
| `image_provider.py` / `agy_image_provider.py` | Provider policy and private account-worker client; production selects `agy` |
| `image_gen.py` | Local Comfy implementation retained for explicit local deployments/rollback |
| `events.py` | Redis publish (`companion:events`) |
| `warmup.py` | Compatibility hooks; load-on-demand policy disables periodic/model prewarming |
| `migrations.py` | Refactor branch: checksum ledger, advisory-locked transactional startup; see migration runbook |

### Production provider policy

- Interactive: explicitly enabled Anthropic Sonnet 5.5, medium adaptive thinking; Dominus is secondary. A local GPU outage does not remove the configured cloud chat primary.
- Dream/Her POV creative text: separately authorized Anthropic route, initially 12 attempts per UTC day and 12 per UTC month. These are attempt limits, not a dollar cap. Other background writers remain local.
- Images: existing Agy/Grok account quotas, alternating preferred providers and at most one eligible service-error failover. Policy refusals do not fail over. Google paid-credit fallback stays disabled; owner confirmed Grok extra usage off/$0. No paid image API or GPU fallback.
- Voice/STT: Dominus services. XTTS uses the pinned character reference and 24 kHz output; no automatic Arc or paid voice fallback. Text remains usable when voice is offline.
- Web: existing authorized Brave MCP search path. This authorization does not extend to other new paid services.
- Fresh checkout defaults differ from private deployment configuration. Use the dated handoffs and verified runtime settings when describing what is deployed.

## events-bridge

Separate small container so core never imports AMQP:

1. Subscribe Redis `companion:events`
2. Forward companion / gpu_lease lifecycle to `homelab.events`
3. Arm deferred delay-bucket queues on `defer.arm`
4. Drain `klukai.jobs.her_pov` (quorum) with hold-until-done → `POST /internal/jobs/her-pov/run`
5. Fire due deferred tasks → `POST /internal/deferred/fire`

Internal routes fail closed without `CORE_INTERNAL_TOKEN`.

## Her POV durability

![Her POV flow](images/her-pov-flow.png)

- Table: `companion_her_pov_jobs` (migration `170`)
- One non-terminal job per user (partial unique index)
- `HER_POV_EXECUTION=queue` in production. Cloud work requires a durable claim and never falls back to inline work after a queue failure. `inline` remains a local-only development path.
- A Redis subscriber count is not proof of durable AMQP acceptance. The refactor acceptance map tracks queued-job reconciliation and bridge heartbeat fixes until verified; do not treat the legacy bridge as a completed outbox.

## Proactive durability

![Deferred rail](images/deferred-rail.png)

- Recurring: APScheduler + `companion_job_runs`
- One-shot: `companion_scheduled` + fixed TTL buckets (no per-message TTL head-of-line trap)
- Sweeper recovers overdue rows if the broker is unavailable

## Three-tier memory

| Tier | Store | Contents |
|---|---|---|
| Session | Redis | Live conversation, mood, mission |
| Episodic | Qdrant | Summaries + embeddings |
| Factual | PostgreSQL | Messages, affection, jobs, archive metadata |

SACRED: never delete chat/memory/affection vectors for cleanup convenience.

## Frontend

- Flutter Web PWA at `/app/` (`--base-href=/app/` required)
- Her POV screen: `flutter_app/lib/screens/her_pov_screen.dart`
- Auth: session token in local storage; login page co-deployed carefully (see deploy scripts)

## Failure domains

| Domain down | User impact |
|---|---|
| GPU host | Local secondary, local background work and voice/STT unavailable; configured Sonnet chat and Agy/Grok pictures remain independent |
| Account image providers | Image request reports provider failure; eligible failover only, no GPU/paid-credit fallback |
| RabbitMQ / bridge | Text chat continues; queued jobs/deferred tasks can be delayed; recovery guarantees depend on the specific durable path |
| Redis | Session/events unavailable or degraded; do not equate a pub/sub receipt with durable work acceptance |
| Postgres | Persistence unavailable; startup must fail on a real migration error |

## Related ADRs

- ADR-0002 Compute split (core / GPU)
- ADR-0004 LM routing (historical local-only decision, superseded by the October 14 owner authorizations above)
- ADR-0006 Image gen / GPU lease
- ADR-0007 Voice on GPU host
- ADR-0009 Public edge / gateway
