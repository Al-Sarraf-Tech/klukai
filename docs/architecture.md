# Klukai Architecture

> Companion PWA + FastAPI core on a two-host private mesh. Provider configuration below reflects the owner's October 14, 2026 deployment. Since October 18, 2026 production runs the exact CI-built core image of `e37e22b` (see [Release and secrets](#release-and-secrets)); the S-tier status per criterion is in the [ledger](handoff/2026-10-16-s-tier-finish.md#s-tier-ledger-honest). Source and tests alone are not deployment evidence. Earlier refactor history is in `handoff/2026-10-14-elite-refactor-progress.md`. Per-decision history lives in `docs/adr/`.

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
| CI heavy guest → core host | No network route; the core host pulls retained artifacts read-only through the GPU host's guest agent, verifies them against a green CI run and the manifest, then signs the provenance |
| Secrets at rest | Compose secrets only as systemd-creds credentials (TPM2 + host key); decrypted to tmpfs for one command; plaintext absent |
| Process crash | Job/defer rows in Postgres survive; queue redelivery is subject to idempotent claims and the no-reclaim rule for ambiguous cloud work |

## companion-core

Single FastAPI process. Important modules:

| Module | Role |
|---|---|
| `chat.py` / `chat_handlers.py` | Authenticated multi-device WebSocket admission and conversation coordination |
| `chat_prompt.py` / `chat_reply.py` / `chat_followups.py` | Prompt preparation, agent/direct reply presentation and follow-up memory work |
| `llm_router.py` / `llm_completions.py` / `llm_intent.py` | Provider selection, transport/streaming and intent classification; Sonnet primary, Dominus secondary |
| `memory_her_pov.py` / `her_pov_*.py` | Durable job board, pipeline, storage and story preparation |
| `deferred.py` | One-shot future tasks (Postgres + bridge timer) |
| `proactive/` | Cron engine + durability catch-up |
| `memory*.py` | Three-tier memory + archive |
| `image_provider.py` / `agy_image_provider.py` | Provider policy and private account-worker client; production selects `agy` |
| `image_gen.py` / `image_*.py` | Local Comfy facade, workflows, rendering and lease ownership retained for explicit local deployments/rollback |
| `runtime_tasks.py` / `ws_manager.py` | Application background-work ownership, admission and cancellation-safe shutdown |
| `shared_http.py` / `observability/health_cache.py` | Lazy HTTP close contract and lifetime-owned readiness clients |
| `dependencies.py` / `circuit_breakers.py` | Exercised dependency boundaries and breaker transitions |
| `events.py` | Redis publish (`companion:events`) |
| `warmup.py` | Compatibility hooks; load-on-demand policy disables periodic/model prewarming |
| `migrations.py` | Checksum ledger and advisory-locked transactional startup; see migration runbook |

### Application lifetime in the uplift

There is one FastAPI process and one application lifetime. Its supervisor owns
background wardrobe, HTTP/backfill, push and portrait work. WebSocket ownership
retains its ordinary and durable task buckets. Existing voice, proactive and
job-handoff components remain the single owners of their children.

Shutdown stops admission, closes producer owners concurrently, then waits for
every producer before unwinding shared HTTP/Redis/router resources and finally
the database pool. A five-second drain precedes cancellation; cancellation joins
asynchronous finalizers and retained native writes/push dispatches. Repeated
cancellation cannot abandon a transport close or a native thread. Partial
startup uses the same resource unwind, and cleanup exceptions retain their
normal context chain. Native/driver completion still requires cooperation;
[declared cleanup deadlines and service allowances](runbooks/encrypted-credentials.md#shutdown-budget)
document that limit.

Accepted or uncertain account-image work keeps its original durable journal,
request ID and prompt. Application shutdown does not reset acceptance or submit
a replacement. A later lifetime recovers the same request. Ordinary browser
disconnection can leave accepted work running; application shutdown still owns
its completion. The Her POV UI separately stops its polling on disposal and
rejects stale job responses without cancelling or resubmitting server work.

The container healthcheck uses `/api/health/live`; required-backend loss must not
cause an autoheal restart loop. Deployment checks `/api/health/ready` separately.
Readiness reuses lifetime-owned clients while executing fresh dependency checks.
PostgreSQL/auth and Redis/session failures fail closed; optional memory/provider
loss degrades without changing the owner's provider/fallback policy.

Per-route RED metrics use bounded route templates. Private `/internal/metrics`
requires its dedicated token file, and sampled traces redact credentials and
correlate requests. `observability/slos.json` drives generated SLO documentation,
rules and dashboard budgets. Actual collector ingestion, alert routing and
retention remain separate live release checks.

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
4. Hold one `klukai.jobs.her_pov` (quorum) delivery while a worker calls `POST /internal/jobs/her-pov/run`
5. Use a separate single worker for due callbacks → `POST /internal/deferred/fire`

The main thread alone owns AMQP: each loop services heartbeats, completions,
bounded due hops and Redis forwarding. Reconnect retains active HTTP slots;
results from an old channel generation cannot ack/nack a new delivery tag.
Jobs and deferred re-parking use publisher confirms with mandatory routing.
Retryable HTTP/claim failures requeue with a five-second cooldown. On shutdown,
unacked deliveries remain with the broker; bounded daemon HTTP workers never
touch channels or assert an unfinished provider outcome.

Internal routes fail closed without `CORE_INTERNAL_TOKEN`.

## Her POV durability

![Her POV flow](images/her-pov-flow.png)

- Table: `companion_her_pov_jobs` (migration `170`); dispatch mark `last_enqueued_at` (migration `200`)
- One non-terminal job per user (partial unique index)
- `HER_POV_EXECUTION=queue` in production. Cloud work requires a durable claim and never falls back to inline work after a queue failure. `inline` remains a local-only development path.
- The committed PostgreSQL row is acceptance. Redis is only a wake-up hint. A core lifecycle task reconciles immediately at startup and every five seconds: at most 16 unstarted queued rows per pass, with a 30-second per-row retry interval. It commits dispatch marks before publication using `FOR UPDATE SKIP LOCKED`, without changing pipeline timestamps or generation attempts.
- Reconciliation excludes started, claimed, attempted and terminal jobs. It republishes the same UUID and does not provide end-to-end exactly-once execution. See [durable job recovery](runbooks/durable-job-handoff.md) for ambiguous outcomes and deployment checks.

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

## Release and secrets

- **Build once.** CI builds the core image, rebuilds it without cache to check reproducibility, scans it (vulnerabilities, secrets, licence policy), writes an SBOM, and retains the image with a digest manifest on the heavy CI guest.
- **Verify and sign.** `ops/amarillo/scripts/klukai-fetch-ci-artifacts.py` pulls it to the core host and verifies it. It then signs `provenance.json` with the core host's release key; the public signer is committed.
- **Deploy with rollback.** `ops/amarillo/scripts/klukai-release.py` deploys by verified image ID after a backup restore check. It rolls back automatically to an immutable tag of the previous image and never touches data.
- **Secrets.** Compose secrets come from the encrypted store through `klukai_compose.py`. Plain `docker compose` lacks them and fails closed.
- Procedures: [release](runbooks/release.md), [encrypted credentials](runbooks/encrypted-credentials.md).

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


### Live session ownership

The core runs a single uvicorn worker. `MemoryManager.session_owner` provides
process-local ownership per session key for connection initialization, complete
text/voice exchanges, and conditional background state patches. It is not a
distributed lock: multiple workers or replicas must not share this write path
without a replacement coordinator. Idle lock entries are reclaimed after both
holders and waiters leave.

Each admitted turn reads Redis inside ownership and persists a new `revision`
before handling input. An unanswered-turn rollback retains that revision, so an
older extraction cannot restore stale mood or mission state. Extraction captures
a deep snapshot and checks conversation identity and revision before applying
Redis mood/mission changes, PostgreSQL mood, timers, or mood broadcasts. Compaction
checks the conversation, original summary, and exact compacted prefix against the
latest stored session, preserving newer turns and unrelated state. Session reads
and writes fail visibly on persistence failure; they do not use socket-local
fallback state. PostgreSQL conversation/message history remains append-only.

WebSocket fan-out snapshots device membership and bounds each send to ten seconds.
Last-device cleanup detaches the old task bucket before awaiting cancellation,
retrieves task failures, and excludes its own sending task. Reconnecting devices
and their newly tracked tasks belong to a new bucket and survive old cleanup.
Failed sends also revoke that device's admission, wake a pending receive, and
attempt transport close with a two-second bound. The endpoint rechecks device
membership after acquiring turn ownership. Terminal failure events remain inside
ownership so they cannot interrupt the next device's reply. Completed assistant
replies append to PostgreSQL before Redis finalization, preserving delivered
history even if the session cache write fails visibly.
