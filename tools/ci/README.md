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
`perf_resources.py`. A worker with registry digests can explicitly supply
`KLUKAI_PERF_POSTGRES_IMAGE`, `KLUKAI_PERF_REDIS_IMAGE`,
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
| `POST /api/tts` | 202 accepted bounded job with a nonempty ID |
| `GET /api/tts/jobs/{job_id}` | Owned completed job contains valid fixture WAV bytes |

The SQL seed includes 100 messages and 20 archives **for each of two owners**;
scope checks reject the other owner's rows. Protected routes must first reject
unauthenticated access with 401. Login rate limits intentionally allow only 10
requests per five minutes; the harness clears only its disposable Redis
`ratelimit:*` keys **outside** each request timer. It applies this same setup to
both inputs, including voice polls. This measures admitted route service time,
not rate limiter saturation or overload behavior. Each accepted voice job must
finish through the deterministic no-charge HTTP fixture before the next
admission. Fixture synthesis and completion wait are outside admission timing.
Only the latest retained completed job is polled in the measured poll route.

This covers process/readiness, bcrypt/session auth, owner-scoped database reads,
and queue admission/polling. It does not claim real LLM, image, voice synthesis,
GPU, websocket streaming, browser, TLS/ingress, archive-image download, production
network, or production-load timing. No image job is submitted. Provider fixtures
perform no external requests and load no model. Existing image/core startup
behavior is preserved; background fixture calls are recorded per round.

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
each paired round and pooled samples. Candidate p99 must be at most 1.20 times
baseline; availability must satisfy the canonical policy for both inputs;
candidate latency must satisfy absolute thresholds from
`docker/core/app/observability/slos.json`. Voice admission has no invented
absolute threshold: it requires 100% observed availability and the ratio gate.
401/404 and incorrect payloads never count as healthy samples. Missing routes,
duplicate routes, nonfinite or zero timing, unequal resource/configuration/source
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
