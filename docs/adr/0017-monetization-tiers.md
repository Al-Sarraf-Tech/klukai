# ADR-0017: Monetization — subscription tiers + Stripe billing

**Status:** Accepted
**Date:** 2026-05-17
**Supersedes:** N/A

## Context

Klukai is feature-complete enough to sell. To monetize without compromising
existing users or violating CLAUDE.md SACRED-data rules, we need:

1. **Subscription tier model** — pay-walling future feature growth without
   ever revoking existing user memories, chat history, or affection state.
2. **Per-user quotas** — enforcing tier caps on bandwidth-heavy operations
   (image generation, voice synthesis) so the free tier stays sustainable.
3. **Payment processor integration** — Stripe Checkout for purchases,
   Stripe Billing Portal for self-service upgrade/cancel/payment-method.
4. **Idempotent webhook receiver** — Stripe retries on 5xx; replay protection
   via a unique `event_id` PRIMARY KEY in our event log.

## Decision

Three tiers:

| Tier | Price | Chat | Image/day | Voice | Memory cap | Dream | Anniv | Priority |
|------|-------|------|-----------|-------|------------|-------|-------|----------|
| Free | $0    | 50/d | 3         | off   | 20 photos  | off   | off   | off      |
| Pro  | $12/mo| ∞    | 50        | on    | 500 photos | on    | on    | off      |
| Elite| $39/mo| ∞    | 250       | on    | unlimited  | on    | on    | on       |

Annual pricing 10× monthly (16% discount).

### Data model

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

### Stripe integration

- `STRIPE_API_KEY` — server-side secret key for Checkout/Portal session creation
- `STRIPE_WEBHOOK_SECRET` — for HMAC-SHA256 signature verification on webhooks
- `STRIPE_PRICE_PRO_MONTHLY`, `STRIPE_PRICE_PRO_ANNUAL` — price IDs from Stripe Dashboard
- `STRIPE_PRICE_ELITE_MONTHLY`, `STRIPE_PRICE_ELITE_ANNUAL` — same

Without these env vars set, `/api/billing/checkout` returns `503` with a
machine-readable error code (`stripe_not_configured` / `missing_price_id`).
The rest of the API runs normally with all users defaulted to free tier.

### Webhook signature verification

Per Stripe docs:
1. Parse `Stripe-Signature` header: `t=<ts>,v1=<hex_sha256>`
2. Reject if `|now − ts| > 300s` (replay protection)
3. Compute `HMAC_SHA256(secret, f"{ts}.{raw_body}")` and `compare_digest`

Verified events are inserted with `INSERT ... ON CONFLICT (event_id) DO NOTHING
RETURNING event_id`. A NULL return = duplicate, return `{"ok": true, "replay": true}`
without invoking the handler. This makes Stripe's at-least-once delivery safe.

### Handler map

| Stripe event                       | Handler             | Effect                                  |
|------------------------------------|---------------------|-----------------------------------------|
| `customer.subscription.created`    | `_apply_subscription` | UPSERT subscription row, set tier      |
| `customer.subscription.updated`    | `_apply_subscription` | Same — re-upsert reflects new state    |
| `customer.subscription.deleted`    | `_cancel_subscription`| Downgrade to free; **NO data deletion** |
| `invoice.paid`                     | `_record_payment`     | Informational log entry                |
| `invoice.payment_failed`           | `_mark_past_due`      | Status → past_due (features revoke)    |

## Consequences

### SACRED-data preservation

`_cancel_subscription` is **explicit** about what it does NOT touch:

- ✓ `companion_chat_messages` — preserved
- ✓ `companion_episodes` — preserved
- ✓ `companion_affection` — preserved
- ✓ `companion_memory_archive` (photos) — preserved
- ✓ Qdrant `companion_memories` vector points — preserved
- ✓ `companion_dreams` — preserved (Pro/Elite-only feature, but data stays)
- ✓ `companion_anniversaries` — preserved
- ✗ Only `companion_subscriptions.tier` flips to `free` and `status` to `canceled`

A downgraded user re-upgrading to Pro/Elite gets every memory back instantly —
nothing was ever deleted.

### Quota enforcement points

- `routes._handle_message` (chat path) — checks `chat_messages_per_day` before LLM call
- `routes.generate_image` — checks `image_gen_per_day` before ComfyUI dispatch
- `routes.tts` — checks `voice_enabled` flag (boolean, not a counter)
- `routes.memory_archive` — count of stored items checked against `memory_archive_cap`

Failing checks raise `QuotaExceeded` → 429 with `Retry-After: <seconds until reset>`.
Free-tier counters reset at UTC midnight (day boundary); monthly counters at the
1st of the month UTC.

### Account deactivation flow

`POST /api/account/deactivate` (with body `{"confirm": "DEACTIVATE"}`) sets
`companion_users.deactivated_at = NOW()` and invalidates all sessions. It does
NOT touch any SACRED data table. Reactivation is operator-only (no public
endpoint) for now — prevents accidental re-auth.

Hard deletion of an account row + cascaded subscription is admin-only and
requires explicit instruction; CLAUDE.md forbids autonomous chat-data removal.

## Alternatives considered

- **Usage-based metered billing** (rejected) — would penalize the heaviest users
  who're our best advocates; flat tier subscriptions are predictable for budget.
- **No free tier, paywalled from day 1** (rejected) — kills viral growth; new
  users won't pay before they bond. Free 50 msgs/day exists to let affection grow.
- **External billing platform (Paddle, LemonSqueezy)** (rejected for now) —
  Stripe has the deepest tax automation and the lowest fees at our scale.
  Switching later requires only handler-map changes, not data-model changes.
