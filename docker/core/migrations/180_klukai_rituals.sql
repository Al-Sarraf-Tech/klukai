-- 180: recurring rituals + read receipts on the ten-year thread
--
-- companion_period_deliveries is the atomic "already delivered this period?"
-- guard for recurring rituals (the Commander's birthday greeting per year, the
-- monthly fee settlement per month). Deliberately NOT companion_firsts: that
-- table feeds anniversaries, stats and the timeline, and yearly/monthly guard
-- rows would surface there as bogus "firsts".
--
-- companion_thread_reads records when the Commander read each entry of the
-- ten-year thread, keyed by the entry's stamp. Both tables are insert-only.

CREATE TABLE IF NOT EXISTS companion_period_deliveries (
    user_id      TEXT NOT NULL,
    ritual       TEXT NOT NULL,
    period       TEXT NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, ritual, period)
);

CREATE TABLE IF NOT EXISTS companion_thread_reads (
    user_id     TEXT NOT NULL,
    entry_stamp TEXT NOT NULL,
    read_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, entry_stamp)
);
