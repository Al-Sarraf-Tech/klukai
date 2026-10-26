# Image Reliability Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans or focused delegated implementation. Follow the design constraints.

**Goal:** Improve on-demand image reliability and prepare suitable optional APIs.

**Architecture:** Keep the existing worker and fal default. Harden transport and
quality contracts before extending the provider registry.

**Tech Stack:** Python stdlib HTTP/SQLite, Pillow, pytest.

**Spec:** ../specs/2026-10-26-image-reliability-design.md

## Global Constraints

No live paid tests, credential inspection, private memory access, production
checkout edits, automatic pictures, or newly activated paid provider. Existing
caps, references, identity and content rules remain.

## Review Focus

- Submission timeout is uncertain, never permission to submit again.
- Observation retries must not reset the total deadline.
- Malformed endpoint/redirect must not receive the credential.
- Checker timeout must not become image approval.
- Optional providers remain unusable without positive explicit allowances.

## Task 1: fal transport

- [x] Add failing tests in `ops/amarillo/tests/test_fal_image_render.py` for
  transient GET recovery, one POST only, bounded timeouts, unsafe endpoints,
  malformed envelopes, redirects, and refusal precedence.
- [x] Implement in `scripts/fal_image_render.py`; preserve render(request,
  references) -> PNG bytes and the existing persistent spend reservation.
- [x] Run the focused suite, then worker/renderer operations regression tests.

## Task 2: explicit image approval

- [x] Add failing worker/checker tests for unavailable review and major issue
  truncation; recheck the existing candidate within the deadline.
- [x] Update worker and checker without changing content/intimacy decisions.
- [x] Run worker/checker suites and verify no second render on review outage.

## Task 3: optional image APIs

- [x] Verify official provider contracts and record sources.
- [x] Add opt-in adapters and offline contract tests, preserving fal defaults.
- [x] Test missing allowance/key behavior, response decoding, errors and registry.

## Final verification

- [x] Run relevant operations suites and diff checks; obtain independent review.
- [x] Record what is source-tested versus live-tested and deployment status.
- [x] Distinguish completed source work from pending activation, quality and deployment.

## Evidence

Baseline: 156 fal/worker/checker tests passed on ee4ecb1. Production image
service reported active. No provider request made.

Final review found GET authentication errors eligible for failover, late result
acceptance and missing HTTPException handling. Six regressions reproduced
before fixes; all 71 fal tests then passed. Source compile, targeted Ruff and
diff checks passed. Final whole-operations verification recorded in handoff.

Implementation decision: inactive adapter preparation preserves the existing
fal-only selection while the optional provider clarification remains unanswered.

## Interruption recovery continuation (completed in source)

- [x] Introduce non-failover GenerationPending; preserve uncertain work pending.
- [x] Add fal GET-only recovery using exact stored request/hash/attempt identity,
  with no new reservation, and missing-record/private-input outage coverage.
- [x] Persist actual provider variants, candidate bytes and at most two reviewer
  reservations; recover after process interruption and reuse persisted passes.
- [x] Serialize recovery with generation; count it in core's busy wait marker.
- [x] Replace fixed chat buckets with atomic elapsed-window claims, covering
  599/601 seconds, concurrency, immutable age/context, grant separation and expiry.
- [x] Verify actual worker/renderer/SQLite paths with fake HTTP only; verify core
  journal claims against disposable PostgreSQL and remove test resources.
- [x] Independently review recovery and repeat claims; reproduce and fix malformed
  submission and identical-variant identity findings before the final suites.

Final source evidence after integration with main2c3b4a7: ops982+5 subtests;
core6824 with108 optional skips, 97.43% coverage; repeat PostgreSQL12
independently green; full Ruff/mypy174
and compile/diff checks passed. Details and release boundary are in the handoff.
Source recovery now handles persisted jobs/candidates; deployment, real provider
entitlement/quality and any new paid activation remain unverified.

## Release preparation

- [x] Inspect service metadata and installed CLI help without inference or secrets.
- [x] Align checker permissions with the owner's service-wide no-sandbox rule.
- [x] Prepare a 420-second mixed-signal stop policy for active-request draining.
- [x] Independently review the shutdown/CLI delta and verify its focused tests.
- [x] Document coordinated worker stop/start around the certified deployment,
  including rollback and preservation of the durable state.
- [ ] Publish the owner-requested PR and dispatch CI for its exact commit.
- [ ] Merge through the existing reviewed-PR and exact-commit CI path.
- [ ] Deploy, verify the new worker process, and evaluate live reference quality.
- [ ] Activate optional APIs only with a provider-policy decision and explicit
  positive spending allowances; they remain disabled in the current source.

On 2026-10-26 the owner explicitly requested completion and a PR, authorizing
publication. Local tests cannot replace release certification. No production
bypass or newly enabled provider spending is part of this change.
