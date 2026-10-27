# Disposable image performance gate

Run from the repository root with the CI Python environment (httpx, psycopg,
Redis, pytest). Docker must be the local Unix daemon. Images must already be
cached immutable IDs or registry digests; this command never pulls an image.
The core images must attest the requested full Git revision in
`org.opencontainers.image.revision`.

```sh
python -m tools.ci.perf_stack \
  --baseline-image "$BASELINE_IMAGE_DIGEST" \
  --candidate-image "$CANDIDATE_IMAGE_DIGEST" \
  --baseline-revision "$BASELINE_SOURCE_REVISION" \
  --candidate-revision "$CANDIDATE_SOURCE_REVISION" \
  --out "$NEW_PRIVATE_EVIDENCE_DIRECTORY"
```

All five arguments are required. The output directory must not exist. Defaults
are 100 measured samples per route per image per round, 10 successful warmups,
3 paired rounds, and concurrency 1. Lower counts and missing thermal sensors
fail. The CPU must remain below 70 C. The baseline/candidate order alternates
between rounds, and only one core runs at a time. Each core uses its image's
default command with identical explicit configuration and fresh PostgreSQL
database, Redis state, and deterministic two-owner seed data.

The PostgreSQL 17, Valkey 8, Qdrant and Python fixture image IDs are pinned in
`perf_resources.py`. Qdrant, `QDRANT_URL`, `LM_STUDIO_URL`/`LM_STUDIO_TOKEN`,
`INFERENCE_URL` and the fixture's LM Studio/embedding routes exist only for the
pre-2026-10-26 baseline (main before the cloud-only core): its readiness needs a
healthy Qdrant and its startup and scheduler call LM Studio and embeddings. The
cloud-only candidate ignores them; remove them once main is cloud-only.

A worker with registry digests can explicitly supply `KLUKAI_PERF_POSTGRES_IMAGE`, `KLUKAI_PERF_REDIS_IMAGE`,
`KLUKAI_PERF_QDRANT_IMAGE` and `KLUKAI_PERF_FIXTURE_IMAGE`; each value must be
immutable and cached. Resolved image IDs enter the fixture fingerprint. Total
container limits are 2.5 CPU and 1728 MiB RAM, including the active core. No
production URL or credential argument exists. Only fresh disposable passwords
enter containers. The private internal network has no external egress or
published ports. A random host loopback TCP proxy connects exclusively to the
created core's private IP. No host directory, home, device, Docker socket, GPU,
host networking or production service is mounted or used. Fixture/configuration
files enter a disposable named volume through standard input to a networkless
writer container.

| Public route | Success contract / fixture coverage |
| --- | --- |
| `GET /health` | 200 and healthy cached real PG/Redis/Qdrant checks |
| `GET /api/health/live` | 200 and live core process |
| `GET /api/health/ready` | 200 and fresh real dependency checks |
| `POST /api/auth/login` | Native password verification and SQL session creation; valid owner and nonempty token |
| `GET /api/auth/verify` | Valid bearer session resolves the seeded owner |
| `GET /api/affection` | Authenticated state has a numeric level |
| `GET /api/messages` | 50 actual SQL messages, all belong to the seeded owner |
| `GET /api/memories` | 20 actual archive metadata records, all belong to that owner |

Voice is not measured. Since 2026-10-26 the core speaks and listens only through
the paid Fish Audio API at a hardcoded endpoint, and CI never calls a paid
provider; without a key the voice routes answer 503. The stack therefore sets no
voice settings and the fixture has no voice or GPU-lease route.

The SQL seed includes 100 messages and 20 archives **for each of two owners**;
scope checks reject the other owner's rows. Protected routes must first reject
unauthenticated access with 401. Login rate limits intentionally allow only 10
requests per five minutes; the harness clears only its disposable Redis
`ratelimit:*` keys **outside** each request timer. It applies this same setup to
both inputs. This measures admitted route service time, not rate limiter
saturation or overload behavior.

This covers process/readiness, bcrypt/session auth and owner-scoped database
reads. It does not claim real LLM, image, voice, GPU, websocket streaming,
browser, TLS/ingress, archive-image download, production network, or
production-load timing. No image or voice job is submitted. Provider fixtures
perform no external requests and load no model. Existing image/core startup
behavior is preserved; background fixture calls are recorded per round. The
measured routes reach no provider, so the evidence validator's proof of actual
fixture work is the startup MCP handshake: every core opens its MCP session
against the fixture before readiness, so each round's `POST /mcp` count must
grow, its `GET /fixture-stats` count must equal the round's position, and no
counter may shrink.

`baseline.json` and `candidate.json` are schema-v1 raw reports; `*-round-N.json`
retain each completed round immediately. Reports bind explicit input references,
resolved running image IDs, requested and OCI revisions, actual Docker quotas,
timestamps, host/tool/configuration/fixture/seed/policy fingerprints, per-sample
HTTP status, finite positive milliseconds and contract success. Payloads,
bearer tokens and passwords are excluded. Container logs and cleanup evidence
remain in the private output directory. Exit is nonzero on missing capability,
warmup failure, unavailable images, missing samples, bad contracts, thermal
limit, nongraceful core shutdown, cleanup failure or gate failure.

`gate.json` recomputes nearest-rank p99 from raw successful samples and checks
each paired round and pooled samples. A route regresses when candidate p99 is
more than 1.20 times baseline and also more than 5 ms slower (`NOISE_FLOOR_MS`).
Nearest-rank p99 of 100 samples is the second-slowest request, so on 2 ms routes
two scheduler hiccups exceed 20%; the floor (owner decision, 2026-10-16) keeps
real regressions such as the +55 ms login tail failing. A route fails the ratio
gate only when it regresses in at least two measured rounds
(`REGRESSED_ROUNDS_REQUIRED`, owner decision 2026-10-17): an A/A nightly failed
on a single noisy `/api/tts` round with a pooled ratio of 1.12. Every row still
records `regressed` for review. Availability must satisfy the canonical policy for both inputs;
candidate latency must satisfy absolute thresholds from
`docker/core/app/observability/slos.json`; policy entries for routes this gate
does not measure (the voice job poll and image generation) are left to
production monitoring. A measured route without a policy entry would need 100%
observed availability and the ratio gate. 401/404 and incorrect payloads never
count as healthy samples. Missing routes, duplicate routes, nonfinite or zero timing, unequal resource/configuration/source
fingerprints, stale evidence and unverified cleanup fail closed. Existing raw
baseline files are never refreshed or overwritten.

The standalone CI/nightly validator uses the same contract and never permits
development reports:

```sh
python -m tools.ci.perf_gate \
  --baseline "$EVIDENCE/baseline.json" --candidate "$EVIDENCE/candidate.json" \
  --baseline-image "$BASELINE_IMAGE_DIGEST" --candidate-image "$CANDIDATE_IMAGE_DIGEST" \
  --baseline-revision "$BASELINE_SOURCE_REVISION" \
  --candidate-revision "$CANDIDATE_SOURCE_REVISION" \
  --policy docker/core/app/observability/slos.json --out "$EVIDENCE/rechecked-gate.json"
```

`--development` on the stack runner permits old or packaging-test images without
matching OCI source labels solely to demonstrate the harness. Every such report
and result is explicitly noncertifying, and `perf_gate` rejects it. A development
run cannot substitute for final baseline/candidate CI or nightly evidence.

Focused validation:

```sh
python -m pytest -q tests/release/test_perf_gate.py
mypy --strict --namespace-packages --explicit-package-bases tools/ci/perf_*.py
ruff check tools/ci/perf_*.py tests/release/test_perf_gate.py
```

## Managed real-backend gate

The heavy guest's host Python orchestrates Docker; the source-bound CI image
runs all ten real-backend cases under CPython 3.14.8 and the complete hashed
CI dependency lock. Supply immutable cached image IDs or repository digests and
the full expected clean Git revision:

```sh
export KLUKAI_CI_OUTPUT="$NEW_PRIVATE_BACKEND_EVIDENCE_DIRECTORY"
export KLUKAI_CI_REVISION="$EXPECTED_SOURCE_REVISION"
export KLUKAI_CORE_CI_IMAGE="$CI_IMAGE_DIGEST"
export KLUKAI_CORE_POSTGRES_IMAGE="$POSTGRES_IMAGE_DIGEST"
export KLUKAI_CORE_REDIS_IMAGE="$VALKEY_IMAGE_DIGEST"
scripts/ci/backends.sh
```

Each of the runtime, readiness and accepted-image shutdown drivers receives a
fresh PostgreSQL 17 and Valkey pair on its own internal network. There is no
vector store: memory lives in PostgreSQL only (cloud-only AI, 2026-10-26), and
the runtime driver stores episodes and exchanges through `MemoryManager`, then
recalls them through the full-text candidate query (migration 260) with the
Claude Haiku pick faked in-process (a pick, an unusable or absent reply that
keeps the PostgreSQL order, and a PostgreSQL loss that fails open and
recovers). Nothing is sent to Anthropic. Services
have no published ports or host mounts. The worker has a read-only committed
source snapshot, a separate read-only generated connection context, and writable
private evidence. It has no Docker socket, owner home, GPU, external provider
credentials or network fallback. Existing TCP fault proxies and all ten driver
assertions remain in the worker. The original opt-in manual Docker fixture mode
remains available for local checks.

The manifest records actual service versions, immutable image and container IDs,
inspected quotas/capabilities, package probe, exact JUnit identities, raw driver
evidence and source hashes. Missing or skipped cases, an altered context/source,
nonzero execution, interrupted work or unverified resource cleanup fail the gate.
The output directory must be new and outside the checkout. Evidence contains
only freshly generated disposable service credentials; preserve its private
permissions. `--development` is explicitly noncertifying.

Validation status: clean checkpoint `4afa6db` completed the then nine
managed-mode real-backend cases (Qdrant included; since removed) in the heavy
guest under CPython 3.14.8, with inspected resources removed and source
unchanged. The retained core coverage database was
also independently checked. This establishes that checkpoint's managed mode;
final-source execution, complete GitHub CI and deployment remain separate
requirements. See the [dated validation evidence](../../docs/handoff/2026-10-15-s-tier-packaging.md).
