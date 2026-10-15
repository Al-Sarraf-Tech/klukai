# Security and account-image recovery fixes

Base: `a6c2f4a1419f0793fe77b6fe849cc09e8f441f14` on `main`.
Scope: review-backed code fixes only. No production deployment, host changes,
provider requests, credentials changes, history deletion, or data repair.

## Changes

1. **WebSocket authorization stays current.** Each public socket binds its
   session hash. Inbound frames, queued-turn admission, and outbound delivery
   revalidate the session owner, expiry, absolute lifetime, and deactivation.
   Auth lookup errors/timeouts fail closed. Password change and deactivation
   retire all local devices immediately; passive sends do not refresh expiry.
2. **Mission context and delivery stay with the owner.** Start, stop, restore,
   prompt injection, reports, aftermath, mood, and affection use per-user
   state. Scheduled jobs retain primary-owner-only context. A missing session
   never falls back to a different connected user. Push notifications specify
   their recipient rather than broadcasting another user's mission.
3. **Definitely unaccepted image requests can be retried safely.** The private
   worker's `/generate` 429 occurs before its durable claim. Core records that
   explicit `busy` state separately, then atomically claims a retry using the
   original journaled payload. Unknown, timed-out, pending, or accepted work
   remains recovery-only. Concurrent/stale recovery cannot erase a busy claim
   or downgrade an archived result.
4. **Recovered pictures retain original provenance.** Migration 220 adds
   `archive_context`; initial acceptance stores conversation, mood, and
   affection alongside the original prompt. API/chat retry archives those
   original values, not current input/state. Legacy rows keep their original
   prompt with neutral missing-context defaults; no historical context is
   invented or rewritten.

## Regression coverage

- `test_ws_auth_revocation.py`: 18 cases, including passive and active
  revocation, all frame kinds, queued turns, expiration, auth outage/timeout,
  owner mismatch, lookup/disconnect races, reconnects, and durable tasks.
- `test_mission_user_isolation.py`: 16 cases, including two owners, primary
  scheduled context, prompt/cancel isolation, callbacks/push, aftermath,
  per-user mood, shutdown, and independent reconnect restoration.
- `test_account_image_retry_protocol.py`: 13 cases, including definite busy,
  uncertain-state no-resubmission, concurrent claims, stale recovery, changed
  retry prompts, API/chat metadata, and the real Worker/SQLite protocol with
  an in-process transport adapter and stub renderer (no provider/network).
- `test_account_image_recovery.py` adds a real-PostgreSQL CAS/provenance test.
  It is present but was not executable in this environment.

The original behavioral regressions were reproduced before implementation.
Independent review caught migration replay safety; migration 220 uses
`ADD COLUMN IF NOT EXISTS` for pre-ledger schema adoption.

## Verification in the isolated checkout

Final core run: **5,079 passed, 85 skipped, 2 deselected, 8 warnings**.
No failures/errors. External sockets were disabled, allowing only loopback
and Unix fixtures; the sandbox separately denied creation of the real Unix
transport test socket, so that one test was explicitly deselected. The other
deselected test is the repository's default live performance exclusion.

```sh
KLUKAI_TEST_ALLOW_LIVE_BACKENDS=0 python -m pytest docker/core/tests -q -rs \
  --timeout=30 --disable-socket --allow-unix-socket \
  --allow-hosts=127.0.0.1,::1 \
  --deselect=tests/test_agy_image_provider.py::test_real_unix_socket_transport_ignores_proxy_environment \
  --deselect=docker/core/tests/test_agy_image_provider.py::test_real_unix_socket_transport_ignores_proxy_environment
```

Test environment: pinned `docker/core/requirements.txt`, pytest 9.0.2,
pytest-asyncio 1.3.0, pytest-timeout, pytest-socket, pytest-cov, Ruff, and socksio
(the environment's HTTP proxy requires socksio even for mocked clients).
Production/new test Ruff, compileall, and `git diff --check` pass. Two existing
F841 diagnostics in `test_chat_handlers_coverage.py` were not introduced or
changed. Existing scheduler/backfill/warmup coroutine warnings remain.

The 85 skips require disposable PostgreSQL or the live integration stack.
No PostgreSQL migration, real Unix transport, live chat/voice, provider,
container build/security scan, or deployment smoke test is claimed.
Before merge/release, run the real PostgreSQL and transport suites in a
permitted disposable environment and inspect CI for the exact PR head.

## Compatibility and operational notes

- Migration 220 is additive and replay-safe. Apply it through the existing
  startup migration runner before serving the new code. Do not use the
  obsolete Alembic command from the generic PR template.
- Existing `busy` failures recorded before this patch are indistinguishable
  from uncertain requests; they are deliberately not auto-resubmitted.
- Password/deactivation eager close is process-local. Other processes reject
  a revoked session on their next send/admission; current deployment uses one
  worker. Already-admitted work may finish, but further delivery revalidates.
- Each outgoing frame/device now performs a DB authorization lookup. Measure
  streaming latency/pool load before deployment; no cache was introduced that
  could silently extend the revocation window.
- No live multi-account exposure was verified. Mission leakage requires a
  second authenticated account/state on the same service. WebSocket
  revocation matters even in an owner-only deployment with a stolen session.
- No rollback/deployment was executed. Any later rollback must preserve the
  applied migration file/ledger and archive data; reverting to a tree missing
  an applied migration intentionally fails the runner's history validation.
