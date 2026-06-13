"""Tests for smarter proactivity — activity-pattern detection, the
pattern-aware "quiet day" check-in, and seasonal/holiday awareness.

All DB access is mocked through the real ``get_conn`` async-contextmanager
API; clocks are frozen via ``datetime.now`` patches; callbacks are AsyncMocks.
Nothing here touches a live PG/LM stack.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.proactive import ProactiveEngine

# A "safe" send window: 15:00, not muted, under cap, last answered.
_AFTERNOON = datetime(2026, 5, 17, 15, 0, 0)  # 2026-05-17 is a Sunday

_DATETIME_TARGETS = (
    "app.proactive.engine.now_local",
    "app.proactive.events.now_local",
    "app.proactive.patterns.now_local",
)


@contextlib.contextmanager
def _patch_now(value: datetime):
    """Freeze datetime.now() across the proactive submodules that bind it."""
    mock_dt = MagicMock(return_value=value)
    with contextlib.ExitStack() as stack:
        for target in _DATETIME_TARGETS:
            stack.enter_context(patch(target, mock_dt))
        yield mock_dt


def _db_ctx(conn):
    """Wrap a connection mock in an async-contextmanager (get_conn style)."""
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _conn_returning(rows):
    """Build a conn whose execute(...).fetchall() yields ``rows``."""
    res = AsyncMock()
    res.fetchall = AsyncMock(return_value=rows)
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=res)
    return conn


# DOW rows are (dow 0..6 [Sun=0], msgs, active_days). A history where every
# weekday is busy EXCEPT Sunday (dow=0), which has zero messages.
def _quiet_sunday_rows():
    return [
        (1, 40, 4),  # Mon
        (2, 40, 4),  # Tue
        (3, 40, 4),  # Wed
        (4, 40, 4),  # Thu
        (5, 40, 4),  # Fri
        (6, 30, 4),  # Sat
        # Sunday (0) absent → 0 messages → strongest quiet signal
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Pattern detection
# ═══════════════════════════════════════════════════════════════════════════


class TestDetectActivityPatterns:
    @pytest.mark.asyncio
    async def test_quiet_sunday_detected_with_high_confidence(self):
        e = ProactiveEngine()
        conn = _conn_returning(_quiet_sunday_rows())
        with patch("app.db.get_conn", return_value=_db_ctx(conn)):
            patterns = await e.detect_activity_patterns("alice")
        assert "quiet_on_sunday" in patterns
        p = patterns["quiet_on_sunday"]
        assert p["type"] == "quiet_day"
        assert p["dow"] == 0
        # Sunday had zero messages → deficit is ~1.0 → max confidence.
        assert p["confidence"] >= 0.9
        # A clearly-busy weekday must NOT be flagged quiet.
        assert "quiet_on_wednesday" not in patterns

    @pytest.mark.asyncio
    async def test_uniform_activity_yields_no_quiet_days(self):
        e = ProactiveEngine()
        rows = [(d, 30, 4) for d in range(7)]  # every weekday equally busy
        conn = _conn_returning(rows)
        with patch("app.db.get_conn", return_value=_db_ctx(conn)):
            patterns = await e.detect_activity_patterns("alice")
        assert patterns == {}

    @pytest.mark.asyncio
    async def test_sparse_window_does_not_flag_quiet_day(self):
        """A weekday seen on fewer than _MIN_WEEKS_OBSERVED occurrences must NOT
        be flagged quiet — even at zero activity. Here the whole history spans a
        single week (max active_days == 1), so a fully-silent Sunday has only
        ~1 observed occurrence, below the floor: no confidence-1.0 from thin air.
        """
        e = ProactiveEngine()
        rows = [
            (1, 10, 1),  # Mon — one busy day
            (2, 10, 1),  # Tue
            (3, 10, 1),  # Wed
            (4, 10, 1),  # Thu
            (5, 10, 1),  # Fri
            (6, 8, 1),   # Sat
            # Sunday (0) absent → 0 messages, but only ~1 week observed
        ]
        conn = _conn_returning(rows)
        with patch("app.db.get_conn", return_value=_db_ctx(conn)):
            patterns = await e.detect_activity_patterns("alice")
        assert "quiet_on_sunday" not in patterns
        assert patterns == {}

    @pytest.mark.asyncio
    async def test_well_observed_low_activity_weekday_is_flagged(self):
        """A weekday observed enough times (>= _MIN_WEEKS_OBSERVED) with clearly
        below-average activity DOES produce a quiet_day, with the expected
        confidence derived from its deficit vs. the overall average."""
        e = ProactiveEngine()
        # 4 weeks of history. Sunday is active on 3 distinct dates (>= 2) but
        # very low volume: 3 msgs over 3 days → per-occurrence avg 1.0.
        rows = [
            (0, 3, 3),   # Sun — observed 3x, low volume
            (1, 40, 4),  # Mon
            (2, 40, 4),  # Tue
            (3, 40, 4),  # Wed
            (4, 40, 4),  # Thu
            (5, 40, 4),  # Fri
            (6, 40, 4),  # Sat
        ]
        conn = _conn_returning(rows)
        with patch("app.db.get_conn", return_value=_db_ctx(conn)):
            patterns = await e.detect_activity_patterns("alice")
        assert "quiet_on_sunday" in patterns
        p = patterns["quiet_on_sunday"]
        assert p["dow"] == 0
        # per-day avgs: Sun=1.0, others=10.0 → overall_avg = (1+10*6)/7 ≈ 8.714.
        # Sun avg 1.0 <= 0.5*8.714 → quiet. deficit = (8.714-1.0)/8.714 ≈ 0.885.
        overall = (1.0 + 10.0 * 6) / 7
        expected_conf = round((overall - 1.0) / overall, 3)
        assert p["confidence"] == pytest.approx(expected_conf, abs=1e-3)
        assert p["user_msgs"] == 3

    @pytest.mark.asyncio
    async def test_empty_history_returns_empty(self):
        e = ProactiveEngine()
        conn = _conn_returning([])
        with patch("app.db.get_conn", return_value=_db_ctx(conn)):
            patterns = await e.detect_activity_patterns("alice")
        assert patterns == {}

    @pytest.mark.asyncio
    async def test_db_error_returns_empty_and_logs(self):
        e = ProactiveEngine()

        def boom(*a, **k):
            raise RuntimeError("db down")

        with patch("app.db.get_conn", side_effect=boom):
            patterns = await e.detect_activity_patterns("alice")
        assert patterns == {}

    @pytest.mark.asyncio
    async def test_result_is_cached_within_ttl(self):
        """A second call inside the TTL must NOT re-query the DB."""
        e = ProactiveEngine()
        conn = _conn_returning(_quiet_sunday_rows())
        gc = MagicMock(return_value=_db_ctx(conn))
        with patch("app.db.get_conn", gc):
            first = await e.detect_activity_patterns("alice")
            second = await e.detect_activity_patterns("alice")
        assert first == second
        assert gc.call_count == 1  # cache hit on the second call

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self):
        e = ProactiveEngine()
        conn = _conn_returning(_quiet_sunday_rows())
        gc = MagicMock(return_value=_db_ctx(conn))
        base = datetime(2026, 5, 17, 12, 0, 0)
        with patch("app.db.get_conn", gc), \
             patch("app.proactive.patterns.now_local") as mock_dt:
            mock_dt.return_value = base
            await e.detect_activity_patterns("alice")
            # Jump >1h forward → cache stale → re-query.
            mock_dt.return_value = datetime(2026, 5, 17, 13, 30, 0)
            await e.detect_activity_patterns("alice")
        assert gc.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# Pattern-aware quiet-day check-in
# ═══════════════════════════════════════════════════════════════════════════


class TestQuietDayCheck:
    @pytest.mark.asyncio
    async def test_fires_on_matching_quiet_day(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 3
        e._last_proactive_answered = True
        e._proactive_count_today = 0
        # Pre-seed the pattern cache so no DB is needed; today (frozen) is Sunday.
        e._pattern_cache = {
            "patterns:alice": (datetime(2026, 5, 17, 15, 0, 0), {
                "quiet_on_sunday": {
                    "type": "quiet_day", "day": "sunday", "dow": 0,
                    "confidence": 0.95, "user_msgs": 0, "overall_avg": 10.0,
                },
            })
        }
        with _patch_now(_AFTERNOON), \
             patch("app.proactive.events.publish_event", new=AsyncMock()):
            await e._quiet_day_check("alice")
        cb.assert_awaited_once()
        msg = cb.call_args.args[0]
        assert "Sunday" in msg
        assert e._quiet_day_delivered_today is True

    @pytest.mark.asyncio
    async def test_skips_when_pattern_weak(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 3
        e._last_proactive_answered = True
        e._pattern_cache = {
            "patterns:alice": (datetime(2026, 5, 17, 15, 0, 0), {
                "quiet_on_sunday": {
                    "type": "quiet_day", "day": "sunday", "dow": 0,
                    "confidence": 0.3,  # below the 0.6 floor
                    "user_msgs": 5, "overall_avg": 7.0,
                },
            })
        }
        with _patch_now(_AFTERNOON):
            await e._quiet_day_check("alice")
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_pattern_day_is_not_today(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 3
        e._last_proactive_answered = True
        # Strong quiet pattern, but for SATURDAY (dow=6) while today is Sunday.
        e._pattern_cache = {
            "patterns:alice": (datetime(2026, 5, 17, 15, 0, 0), {
                "quiet_on_saturday": {
                    "type": "quiet_day", "day": "saturday", "dow": 6,
                    "confidence": 0.95, "user_msgs": 0, "overall_avg": 10.0,
                },
            })
        }
        with _patch_now(_AFTERNOON):
            await e._quiet_day_check("alice")
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_affection_gate_blocks_strangers(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 0  # too cold to comment on silence
        e._last_proactive_answered = True
        e._pattern_cache = {
            "patterns:alice": (datetime(2026, 5, 17, 15, 0, 0), {
                "quiet_on_sunday": {
                    "type": "quiet_day", "day": "sunday", "dow": 0,
                    "confidence": 0.95, "user_msgs": 0, "overall_avg": 10.0,
                },
            })
        }
        with _patch_now(_AFTERNOON):
            await e._quiet_day_check("alice")
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_once_per_day_guard(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 3
        e._last_proactive_answered = True
        e._quiet_day_delivered_today = True  # already fired today
        e._pattern_cache = {
            "patterns:alice": (datetime(2026, 5, 17, 15, 0, 0), {
                "quiet_on_sunday": {
                    "type": "quiet_day", "day": "sunday", "dow": 0,
                    "confidence": 0.95, "user_msgs": 0, "overall_avg": 10.0,
                },
            })
        }
        with _patch_now(_AFTERNOON):
            await e._quiet_day_check("alice")
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blocked_by_can_send_when_muted(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 5
        e._last_proactive_answered = True
        e._muted_until = datetime(2099, 1, 1)
        e._pattern_cache = {
            "patterns:alice": (datetime(2026, 5, 17, 15, 0, 0), {
                "quiet_on_sunday": {
                    "type": "quiet_day", "day": "sunday", "dow": 0,
                    "confidence": 0.95, "user_msgs": 0, "overall_avg": 10.0,
                },
            })
        }
        with _patch_now(_AFTERNOON):
            await e._quiet_day_check("alice")
        cb.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# Seasonal / holiday awareness
# ═══════════════════════════════════════════════════════════════════════════

_SEASONAL_CFG = {
    "seasonal_events": {
        "valentines": {
            "month": 2, "day": 14, "min_affection": 2,
            "messages": ["Valentine's, Commander. Chocolate's on your desk."],
        },
        "christmas": {
            "month": 12, "day": 25, "min_affection": 0,
            "messages": ["Merry Christmas, Commander."],
        },
    }
}


class TestSeasonalCheck:
    @pytest.mark.asyncio
    async def test_fires_on_matching_date(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 5
        with patch("app.proactive.events.now_local") as mock_dt, \
             patch("app.personality.load_personality", return_value=_SEASONAL_CFG), \
             patch("app.proactive.events.publish_event", new=AsyncMock()):
            mock_dt.return_value = datetime(2026, 12, 25, 9, 0, 0)
            await e._seasonal_check()
        cb.assert_awaited_once()
        assert "Christmas" in cb.call_args.args[0]

    @pytest.mark.asyncio
    async def test_no_fire_on_non_matching_date(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 5
        with patch("app.proactive.events.now_local") as mock_dt, \
             patch("app.personality.load_personality", return_value=_SEASONAL_CFG):
            mock_dt.return_value = datetime(2026, 7, 4, 9, 0, 0)  # July 4
            await e._seasonal_check()
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_affection_gate_suppresses_valentines(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 1  # below valentines min_affection=2
        with patch("app.proactive.events.now_local") as mock_dt, \
             patch("app.personality.load_personality", return_value=_SEASONAL_CFG):
            mock_dt.return_value = datetime(2026, 2, 14, 9, 0, 0)
            await e._seasonal_check()
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fires_once_per_occurrence(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 5
        with patch("app.proactive.events.now_local") as mock_dt, \
             patch("app.personality.load_personality", return_value=_SEASONAL_CFG), \
             patch("app.proactive.events.publish_event", new=AsyncMock()):
            mock_dt.return_value = datetime(2026, 12, 25, 9, 0, 0)
            await e._seasonal_check()
            await e._seasonal_check()  # same day → guarded
        cb.assert_awaited_once()
        # The guard key for this occurrence is recorded.
        assert e._seasonal_delivered.get("christmas:2026-12-25") is True

    @pytest.mark.asyncio
    async def test_blocked_when_muted(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 5
        e._muted_until = datetime(2099, 1, 1)
        with patch("app.proactive.events.now_local") as mock_dt, \
             patch("app.personality.load_personality", return_value=_SEASONAL_CFG):
            mock_dt.return_value = datetime(2026, 12, 25, 9, 0, 0)
            await e._seasonal_check()
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_config_load_error_swallowed(self):
        e = ProactiveEngine()
        cb = AsyncMock()
        e._on_message_callback = cb
        e._affection_level = 5
        with patch("app.proactive.events.now_local") as mock_dt, \
             patch("app.personality.load_personality", side_effect=RuntimeError("no cfg")):
            mock_dt.return_value = datetime(2026, 12, 25, 9, 0, 0)
            await e._seasonal_check()  # must not raise
        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_real_personality_yaml_has_seasonal_events(self):
        """Sanity: the shipped config exposes the events the job consumes."""
        from app.personality import load_personality
        cfg = load_personality().get("seasonal_events", {})
        assert "christmas" in cfg
        assert cfg["christmas"]["month"] == 12 and cfg["christmas"]["day"] == 25
        assert "new_year" in cfg and cfg["new_year"]["month"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Scheduler wiring + daily reset
# ═══════════════════════════════════════════════════════════════════════════


class TestSchedulerWiring:
    @pytest.mark.asyncio
    async def test_new_jobs_registered_on_start(self):
        # Async so AsyncIOScheduler.start() has a running event loop — the same
        # context the engine starts in at runtime (and so the test is stable
        # regardless of sibling-test loop teardown order).
        e = ProactiveEngine()
        try:
            e.start()
            job_ids = {j.id for j in e._scheduler.get_jobs()}
            assert "seasonal_check" in job_ids
            assert "quiet_day_check" in job_ids
        finally:
            e.stop()

    @pytest.mark.asyncio
    async def test_reset_daily_clears_smart_flags(self):
        e = ProactiveEngine()
        e._quiet_day_delivered_today = True
        e._seasonal_delivered = {"christmas:2026-12-25": True}
        await e._reset_daily()
        assert e._quiet_day_delivered_today is False
        assert e._seasonal_delivered == {}
