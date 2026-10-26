# ADR-0019: Recover paid image attempts without resubmission

- **Date:** 2026-10-26
- **Status:** Proposed

## Context

An interrupted image request can leave a paid fal job running, or a rendered
candidate waiting for Claude review. Retrying generation in either case can
charge for another picture and lose the original candidate. A checker outage
also must not turn an unchecked image into an approved one.

## Decision

Persist each worker attempt's actual payload, candidate and bounded review
reservations. Persist fal's exact remote job identity separately in its attempt
ledger. Recovery authenticates the original request and observes that same job,
or resumes review of its saved candidate. It never submits another image,
reserves another generation charge, cancels a job or switches providers.

Unknown submission outcomes remain pending. Retry transient observation GETs
within a fixed deadline; never retry the submission POST. Require an explicit,
consistent checker pass, with at most two review reservations per candidate
including interrupted calls. Reuse a persisted approval after restart.

Protect repeated human picture requests with an atomic elapsed 600-second claim
in the existing core journal. Preserve the original context, grant and age.

## Consequences

- A missing job handle or a legacy pending row without recovery metadata can
  remain unresolved. This avoids guessing that paid work never started.
- Recovery can perform bounded reviewer inference. It is observational with
  respect to image generation, not a promise of no state updates or no review.
- Additive worker SQLite tables retain image-job context and candidate bytes;
  preserve this state and the fal ledger across rollout and rollback. Existing
  chat and memory data are unaffected, and no core schema migration is needed.
- A graceful worker stop must drain request threads and their checker children
  before closing state. The deployment coordinator must restart this host worker
  explicitly around the certified source rollout.
- Optional OpenAI/Gemini adapters obey the same no-resubmission contract, but
  cannot recover unknown synchronous submissions. They remain disabled with
  zero default allowances; the existing fal-only production decision is unchanged.

## Alternatives

Automatic resubmission or cross-provider failover after uncertainty cannot
prove that the original paid attempt stopped. Approving images after checker
failure would discard the quality requirement. Both are rejected.

See the [implementation design](../superpowers/specs/2026-10-26-image-reliability-design.md)
and [validation and rollout handoff](../handoff/2026-10-26-image-reliability.md).
