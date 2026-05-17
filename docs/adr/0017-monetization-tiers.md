# ADR-0017: Monetization scaffold — tier model + Stripe webhook (dormant)

**Status:** Accepted (scaffold only — activation surface removed 2026-05-17)
**Date:** 2026-05-17
**Supersedes:** N/A

## Context

Klukai is feature-complete enough to sell, but the current deployment is for
the operator's personal use. The author wants the *data model* and webhook
plumbing ready so monetization can be activated later by setting a single
environment variable — without rebuilding tables or rewriting routes.

## Decision

**Dormant scaffold pattern.** Keep everything except the activation surface:

| Layer                        | State    | Reason                                |
|------------------------------|----------|---------------------------------------|
| `companion_subscriptions`    | Present  | Tier model needs a home in the schema |
| `companion_usage_counters`   | Present  | Quota infra needs a home in the schema |
| `companion_stripe_events`    | Present  | Webhook idempotency must persist      |
| `app/billing.py`             | Present  | TIER_FEATURES, quota helpers, sig verify |
| `/api/billing/tiers`         | Present (info-only) | Public feature matrix view |
| `/api/billing/subscription`  | Present  | Auth user reads own tier              |
| `/api/billing/usage`         | Present  | Auth user reads own usage             |
| `/api/billing/webhook`       | Present  | HMAC-verified, 400s without secret    |
| `/api/billing/checkout`      | **REMOVED** | No user-facing "Subscribe" path now |
| `/api/billing/portal`        | **REMOVED** | No Stripe portal redirect now       |
| `BillingCheckoutRequest`     | Removed  | No longer wired to any route          |
| Subscribe buttons in Flutter | Removed  | UI shows feature/usage only           |
| `PRICING` ($USD constants)   | Anonymized | No price strings — generic bullets only |

### KLUKAI_PERSONAL_MODE env flag (default: `true`)

`app.billing.get_subscription(user_id)` short-circuits when personal mode is
on, returning `Subscription(tier="elite", status="active")` without touching
the database. `consume_quota` likewise bypasses all caps.

This means:

- Every authenticated user (the operator + family in personal mode) has
  every feature on.
- The `companion_subscriptions` rows still exist (backfilled to `free`) but
  are inert until personal mode is flipped off.
- Tests can flip the env via `monkeypatch.setenv("KLUKAI_PERSONAL_MODE", "false")`
  to exercise the tier-aware code paths.

### What it takes to flip on monetization later

1. `KLUKAI_PERSONAL_MODE=false`
2. Provision `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, price IDs in env
3. Restore `/api/billing/checkout` + `/api/billing/portal` endpoints
   (see this ADR's git history for the original code)
4. Restore `BillingCheckoutRequest` model
5. Re-add Subscribe buttons in `subscription_screen.dart`
6. Run `INSERT INTO companion_subscriptions ...` for any newly-paying user
   (or let `_apply_subscription` webhook handler do it on first Stripe event)

The webhook receiver is already live and signature-verified — it 400s today
because no `STRIPE_WEBHOOK_SECRET` is set, but the handler dispatch table,
the `companion_stripe_events` idempotency log, and the
`_apply_subscription` / `_cancel_subscription` / `_record_payment` /
`_mark_past_due` handlers are all in place.

## Tier matrix (informational — dormant in personal mode)

| Tier | Chat | Image/day | Voice | Memory cap | Dream | Anniv | Priority |
|------|------|-----------|-------|------------|-------|-------|----------|
| Free | 50/d | 3         | off   | 20 photos  | off   | off   | off      |
| Pro  | ∞    | 50        | on    | 500 photos | on    | on    | off      |
| Elite| ∞    | 250       | on    | unlimited  | on    | on    | on       |

When personal mode is on, every user is treated as elite regardless of their
`companion_subscriptions.tier` row.

## Data model

```sql
companion_subscriptions:
  user_id PRIMARY KEY (FK companion_users.id ON DELETE CASCADE)
  tier {'free','pro','elite'}
  status {'active','past_due','canceled','paused','trialing'}
  period_start, period_end TIMESTAMPTZ
  stripe_customer_id, stripe_subscription_id TEXT
  created_at, updated_at TIMESTAMPTZ

companion_stripe_events:
  event_id PRIMARY KEY
  event_type, payload JSONB
  received_at, processed, error

companion_usage_counters:
  PK (user_id, counter_name, period_key)
  count INTEGER
  last_used_at TIMESTAMPTZ
```

## Webhook signature verification (preserved for future use)

Per Stripe docs:
1. Parse `Stripe-Signature` header: `t=<ts>,v1=<hex_sha256>`
2. Reject if `|now − ts| > 300s` (replay protection)
3. Compute `HMAC_SHA256(secret, f"{ts}.{raw_body}")` and `compare_digest`

Verified events are inserted with `INSERT ... ON CONFLICT (event_id) DO NOTHING
RETURNING event_id`. A NULL return = duplicate, return `{"ok": true, "replay": true}`
without invoking the handler. This makes Stripe's at-least-once delivery safe.

## SACRED-data invariant (CLAUDE.md absolute rule)

`_cancel_subscription` is **explicit** about what it does NOT touch when
monetization is eventually activated:

- ✓ `companion_chat_messages` — preserved
- ✓ `companion_episodes` — preserved
- ✓ `companion_affection` — preserved
- ✓ `companion_memory_archive` (photos) — preserved
- ✓ Qdrant `companion_memories` vector points — preserved
- ✓ `companion_dreams` — preserved
- ✓ `companion_anniversaries` — preserved
- ✗ Only `companion_subscriptions.tier` flips to `free` and `status` to `canceled`

A downgraded user re-upgrading to Pro/Elite gets every memory back instantly —
nothing was ever deleted.

## Alternatives considered

- **Delete the scaffold entirely** (rejected) — would force a full migration
  + rewrite when monetization is wanted later; cheap to keep dormant.
- **Gate by feature flag library (Unleash, GrowthBook)** (rejected) — extra
  service dependency for a single boolean.
- **In-code constant** (rejected) — requires rebuild to flip; env var is
  better-suited.
- **Usage-based metered billing** (rejected) — would penalize the heaviest
  users; flat tier subscriptions are predictable for budget.

## Consequences

- Self-hosted deployment defaults to `KLUKAI_PERSONAL_MODE=true` via compose env
- Operator can flip the flag in one place to test tier-aware paths without
  losing personal-use convenience
- Stripe SDK is no longer imported anywhere — removes a heavy dependency at
  runtime while activation is off
- Future paying user does NOT need a schema migration — just env config
