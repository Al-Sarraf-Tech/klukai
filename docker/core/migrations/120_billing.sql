-- Monetization: subscription tiers + usage counters
-- ADR-0017
--
-- Tier model:
--   free  — chat (limited), image_gen (3/day), voice off, memory_archive cap 20
--   pro   — chat (unlimited), image_gen (50/day), voice on, memory cap 500
--   elite — chat (unlimited), image_gen (250/day), voice on, memory unlimited, dream diary, anniversaries
--
-- SACRED invariant: NEVER delete chat memories on subscription cancel.
-- Tier downgrade only revokes feature access; existing data persists.

CREATE TABLE IF NOT EXISTS companion_subscriptions (
    user_id TEXT PRIMARY KEY REFERENCES companion_users(id) ON DELETE CASCADE,
    tier TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'pro', 'elite')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
        'active', 'past_due', 'canceled', 'paused', 'trialing'
    )),
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_subs_stripe_customer
    ON companion_subscriptions(stripe_customer_id)
    WHERE stripe_customer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_subs_stripe_sub
    ON companion_subscriptions(stripe_subscription_id)
    WHERE stripe_subscription_id IS NOT NULL;

-- Stripe event log — idempotency + audit
CREATE TABLE IF NOT EXISTS companion_stripe_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL,
    processed BOOLEAN NOT NULL DEFAULT FALSE,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_stripe_events_unprocessed
    ON companion_stripe_events(received_at)
    WHERE processed = FALSE;

-- Per-user rolling usage counters. Reset by background task at period boundary.
CREATE TABLE IF NOT EXISTS companion_usage_counters (
    user_id TEXT NOT NULL REFERENCES companion_users(id) ON DELETE CASCADE,
    counter_name TEXT NOT NULL,
    period_key TEXT NOT NULL,         -- e.g. '2026-05-17' (daily) or '2026-05' (monthly)
    count INTEGER NOT NULL DEFAULT 0,
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, counter_name, period_key)
);

CREATE INDEX IF NOT EXISTS idx_usage_recent
    ON companion_usage_counters(last_used_at);

-- Backfill: every existing user gets a 'free' subscription row
INSERT INTO companion_subscriptions (user_id, tier, status)
SELECT id, 'free', 'active'
FROM companion_users
ON CONFLICT (user_id) DO NOTHING;
