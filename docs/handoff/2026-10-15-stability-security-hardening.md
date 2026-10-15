# Stability and security hardening

Base: `df5cc96bff1cc2cd67972ec26d7a0f6dd4c0e10a`.
Branch: `codex/stability-security-20261015`.

The owner initially authorized fixes and local commits with Amarillo git-server
sync, then explicitly instructed: “get them healthy and deploy then.” The patch
was developed and verified in an isolated worktree. Rollout preserves runtime
credentials, provider settings, historical records and migration files.
Disposable PostgreSQL checks use only `KLUKAI_MIGRATION_TEST_DATABASE_URL`.

## Requirements and evidence bindings

| ID | Required behavior | Implementation | Verification |
| --- | --- | --- | --- |
| CRED-1 | Query credentials and bearer values stay out of server logs, trace exports and scheduler error audits; authentication still receives its original token. | `observability/redaction.py`, `observability/trace_redaction.py`, tracing initialization, scheduler error listener | `test_credentials_observability.py` exercises actual Uvicorn logger routing, formatted/mapping arguments, exception and cached traceback text, encoded query keys, nonsecret keys, real FastAPI instrumentation and the configured export pipeline, event/status/sequence attributes and original-span preservation. `test_proactive_shutdown.py` verifies sanitized audit metadata. |
| AUTH-1 | A concurrent password reset or deactivation cannot issue a new session from an already-observed stale credential. Only one concurrent reset may use the old password. | `auth.authenticate` verifies outside a database connection, then locks and rechecks the exact credential inside the session-issuance transaction. `change_password` uses a conditional update and revokes sessions in that same transaction. Device revocation follows commit. | Five cases in `test_auth_concurrency.py` use real isolated PostgreSQL schemas and controlled interleavings, including actual off-loop bcrypt verification and failed-attempt recording for oversized UTF-8 input. Existing session revocation suites remain required. |
| AUTH-2 | Password CPU work and physical executor backlog remain bounded during cancellation and saturation. Invalid input is never silently truncated. | `passwords.py`: two worker threads, eight outstanding operations. Cancelled callers retain their physical slot until work finishes. Saturation returns `503` and `Retry-After`; new passwords are limited to 72 UTF-8 bytes. | `test_password_work.py`, `test_auth_capacity_routes.py`; existing authentication tests exercise real bcrypt hashes. |
| WS-1 | Connection admission is bounded across simultaneous and failed handshakes, initial sends, cancellation and slow rejection closes. | `WSManager`: eight admitted devices per user; 64 total sockets including pending and rejection handshakes. Per-user overflow receives `1013` when room remains for its handshake. At the hard global bound the server denies before acceptance (HTTP 403 under Uvicorn). Existing clients back off; only authentication-specific close codes log them out. | `test_ws_capacity.py`, existing protocol and revocation tests. Twelve simultaneous overflow attempts cannot accept new sockets behind a stalled rejection. The Flutter service resets backoff only after a real server frame. |
| LIFE-1 | Partial startup and shutdown errors release already-owned resources; maintenance ends before the database closes. | `main.lifespan` uses an asynchronous exit stack. The session janitor is cancelled and joined. Proactive catch-up, scheduler execution, audit/bookkeeping and mission tasks have explicit ownership and asynchronous shutdown. Separate task sets preserve nested catch-up ownership; closing rejects new spawns. Cancellation is requested once so asynchronous finalizers can finish. Memory/router cleanup attempts both clients even if the first close fails or is cancelled. | `test_lifespan_cleanup.py`, `test_proactive_shutdown.py` and `test_client_cleanup.py` cover partial initialization, cleanup faults, real engine catch-up at database close, real APScheduler job finalizers, nested jobs, stopped-task admission and scheduler shutdown failures. |

## Verification procedure

The initial focused baseline passed 102 tests. New regressions were observed
failing before their fixes, including five real PostgreSQL authentication
interleavings. Independent review reproduced exception-event leakage,
uncounted rejection handshakes and catch-up surviving database shutdown;
those cases received regression tests and fixes. The final independent review
passed 48 targeted tests and reported no further blockers; it separately
passed the five PostgreSQL concurrency cases.

The final full core run passed **5,167 tests**, with 57 skipped, one deselected,
seven warnings and **96.39% coverage**, above the unchanged 95% gate. Ruff and
Mypy passed (108 production source files). Bandit found no medium/high issues;
the dependency audit found no known vulnerabilities in 74 locked dependencies.
The warnings include existing test coroutine/resource warnings and the
Starlette TestClient deprecation. The final listener and wrapper-binding changes
also passed a separate three-test reviewer check.

Run the complete core suite against a disposable database with the existing
95% coverage gate and skip ceiling retained. An outer test timeout must exceed
the migration runner's deliberate 30-second advisory-lock test. The first
local run incorrectly used `--timeout=30`; the isolated recovery test passed
with `--timeout=60`. A subsequent run exposed a scheduler-wiring assertion
that required the original handler directly; it now verifies the wrapper's
retained handler binding and closes its engine.

```sh
KLUKAI_TEST_ALLOW_LIVE_BACKENDS=0 \
KLUKAI_MIGRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:PORT/klukai_hardening \
  python -m pytest tests/ -q --tb=short --timeout=60 \
  --cov=app --cov-report=term-missing --cov-fail-under=95
ruff check app/ --config ruff.toml
mypy app/ --config-file mypy.ini
bandit -r app/ -ll --skip B101
pip-audit --requirement requirements.txt --disable-pip --no-deps
```

Complete command output is retained privately under
`~/.local/state/klukai/hardening-20261015/`. The final run report records the
actual full-suite, coverage, scan, commit and mirror results. Tool configuration
and live service credentials are excluded from Git. Three legitimate source
and test files match the owner's broad global Git secret-name ignores; they
must remain explicitly tracked, including `app/passwords.py`.

## Deployment and limits

There are no schema migrations or dependency changes in this patch. Existing
session hashes, password hashes, archive journals and provider controls retain
their formats. History and image claims remain untouched. Scheduler shutdown
does not invent a retry or resubmit accepted image work.

Limits and session coordination remain process-local; this patch does not
authorize multiple core workers. Sanitization protects future emitted data;
it does not rewrite old logs, traces or sealed audit rows. Local tests do not
establish live provider or browser acceptance. Rollout results, image identity,
backup and live checks are recorded separately after activation.
