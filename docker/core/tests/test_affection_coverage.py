"""Behavioral coverage tests for app.affection — the affection-score engine.

Every test asserts a concrete behavior: a specific score delta, a level
boundary, a daily-cap clamp, a classification mapping, an absence-decay
amount, or a persisted SQL effect. No test merely "calls" a function.

All external dependencies are mocked deterministically:
  * The PostgreSQL pool (get_conn / get_conn_autocommit) is replaced with an
    async context manager whose execute/fetchone are fully under test control.
  * The classification LLM (httpx + llm_router gate) is mocked.
  * date.today() / datetime.now() are frozen via patch so deltas, daily-cap
    resets, consecutive-day streaks and decay windows are reproducible.

This file is intentionally independent of conftest's MagicMock affection
fixtures — it constructs real AffectionState/AffectionManager objects so the
production code paths (_apply_delta, _compute_level, decay, persistence) run
for real.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("psycopg")

from app.affection import (  # noqa: E402
    DAILY_POINTS_CAP,
    MAX_SCORE,
    AffectionChange,
    AffectionManager,
    AffectionState,
)

# Real production level thresholds (config/personality.yaml). Used both to
# configure the manager and to assert level boundaries match the live config.
LEVELS = [
    {"index": 0, "threshold": 0, "name": "Cold Assessment"},
    {"index": 1, "threshold": 30, "name": "Acknowledged"},
    {"index": 2, "threshold": 80, "name": "Professional Respect"},
    {"index": 3, "threshold": 150, "name": "Guarded Interest"},
    {"index": 4, "threshold": 250, "name": "Trusted Ally"},
    {"index": 5, "threshold": 380, "name": "Unguarded"},
    {"index": 6, "threshold": 530, "name": "Deep Devotion"},
    {"index": 7, "threshold": 680, "name": "Vulnerable"},
    {"index": 8, "threshold": 830, "name": "Bonded"},
    {"index": 9, "threshold": 950, "name": "Oath Fulfilled"},
]


# ── DB connection mock ───────────────────────────────────────────────────────


def _make_conn(fetchone_result="UNSET"):
    """Return (ctx_factory, conn) where ctx_factory works as get_conn /
    get_conn_autocommit replacement.

    conn.execute is an AsyncMock returning a cursor-like result whose
    fetchone() is awaitable — matching `await (await conn.execute(...)).fetchone()`.
    conn.commit / conn.rollback are AsyncMocks.
    """
    result = AsyncMock()
    if fetchone_result != "UNSET":
        result.fetchone = AsyncMock(return_value=fetchone_result)

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)
    conn.commit = AsyncMock()
    conn.rollback = AsyncMock()

    @asynccontextmanager
    async def _ctx(*args, **kwargs):
        yield conn

    return _ctx, conn


def _patch_db(fetchone_result="UNSET"):
    """Patch both get_conn and get_conn_autocommit in app.affection.

    Returns a context manager that, on enter, yields the shared conn mock.
    """
    ctx_factory, conn = _make_conn(fetchone_result=fetchone_result)

    class _DBPatch:
        def __enter__(self):
            self._p1 = patch("app.affection.get_conn", ctx_factory)
            self._p2 = patch("app.affection.get_conn_autocommit", ctx_factory)
            self._p1.start()
            self._p2.start()
            return conn

        def __exit__(self, *exc):
            self._p1.stop()
            self._p2.stop()
            return False

    return _DBPatch()


@pytest.fixture
def manager(personality_config_path, monkeypatch):
    """A manager with real levels and personality.yaml pointed at the repo copy.

    Clears the personality cache so PERSONALITY_PATH takes effect, then seeds
    _levels with the production thresholds.
    """
    monkeypatch.setenv("PERSONALITY_PATH", personality_config_path)
    import app.personality as _p

    _p._PERSONALITY = None
    _p._PERSONALITY_PATH = ""
    mgr = AffectionManager()
    mgr._levels = [dict(lv) for lv in LEVELS]
    return mgr


# A fixed "today" used to make daily-cap / streak / decay logic deterministic.
FIXED_TODAY = date(2026, 5, 20)
FIXED_NOW = datetime(2026, 5, 20, 12, 0, 0)


@pytest.fixture
def freeze_time():
    """Freeze app.affection.date.today() and datetime.now() to FIXED_TODAY/NOW.

    affection.py imports `from datetime import date, datetime`, so we patch the
    names in the affection module namespace. We subclass to keep date/datetime
    arithmetic (subtraction, .days) working normally.
    """

    class _FixedDate(date):
        @classmethod
        def today(cls):
            return FIXED_TODAY

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FIXED_NOW

    with patch("app.affection.date", _FixedDate), patch(
        "app.affection.datetime", _FixedDateTime
    ):
        yield


# ── _compute_level: score → (level, name) boundaries ─────────────────────────


class TestComputeLevel:
    def test_zero_is_cold_assessment(self, manager):
        assert manager._compute_level(0) == (0, "Cold Assessment")

    def test_below_first_threshold_stays_level_zero(self, manager):
        assert manager._compute_level(29) == (0, "Cold Assessment")

    def test_exact_threshold_promotes(self, manager):
        # 30 is the exact threshold for level 1.
        assert manager._compute_level(30) == (1, "Acknowledged")

    def test_just_below_threshold_does_not_promote(self, manager):
        assert manager._compute_level(79) == (1, "Acknowledged")
        assert manager._compute_level(80) == (2, "Professional Respect")

    def test_max_score_is_top_level(self, manager):
        assert manager._compute_level(1000) == (9, "Oath Fulfilled")

    def test_over_max_clamps_to_top_level(self, manager):
        assert manager._compute_level(5000) == (9, "Oath Fulfilled")

    def test_empty_levels_returns_default(self):
        mgr = AffectionManager()
        mgr._levels = []
        assert mgr._compute_level(999) == (0, "Cold Assessment")

    def test_progression_is_monotonic(self, manager):
        prev = -1
        for score in range(0, 1001, 5):
            lv, _ = manager._compute_level(score)
            assert lv >= prev
            prev = lv


# ── _calculate_delta: type+intensity → points (intensity scaling) ────────────


class TestCalculateDelta:
    def test_greeting_scalar_is_one(self, manager):
        # greeting: 1 (scalar in config) — intensity irrelevant.
        assert manager._calculate_delta("greeting", 1) == 1
        assert manager._calculate_delta("greeting", 9) == 1

    def test_neutral_is_zero(self, manager):
        # neutral maps to None alias → 0 points.
        assert manager._calculate_delta("neutral", 5) == 0

    def test_unknown_type_is_zero(self, manager):
        assert manager._calculate_delta("does_not_exist", 7) == 0

    def test_compliment_scales_with_intensity(self, manager):
        # compliment: [2, 5]. intensity 1 → low end (2); intensity 10 → high (5).
        assert manager._calculate_delta("compliment", 1) == 2
        assert manager._calculate_delta("compliment", 10) == 5
        mid = manager._calculate_delta("compliment", 5)
        assert 2 <= mid <= 5

    def test_genuine_interest_range(self, manager):
        # genuine_interest: [1, 3].
        assert manager._calculate_delta("genuine_interest", 1) == 1
        assert manager._calculate_delta("genuine_interest", 10) == 3

    def test_remembering_alias_maps_to_remembering_details(self, manager):
        # "remembering" → "remembering_details": [2, 4].
        assert manager._calculate_delta("remembering", 1) == 2
        assert manager._calculate_delta("remembering", 10) == 4

    def test_rude_alias_is_negative(self, manager):
        # "rude" → "rude_language": [-3, -8]. intensity 1 → -3; intensity 10 → -8.
        assert manager._calculate_delta("rude", 1) == -3
        assert manager._calculate_delta("rude", 10) == -8

    def test_inappropriate_alias_is_strongly_negative(self, manager):
        # "inappropriate" → "inappropriate_content": [-5, -10].
        assert manager._calculate_delta("inappropriate", 1) == -5
        assert manager._calculate_delta("inappropriate", 10) == -10

    def test_ignoring_advice_alias(self, manager):
        # "ignoring_advice" → "ignoring_advice": [-2, -5].
        assert manager._calculate_delta("ignoring_advice", 1) == -2
        assert manager._calculate_delta("ignoring_advice", 10) == -5


# ── get_state: jalsarraf pin vs other-user DB load ───────────────────────────


class TestGetState:
    @pytest.mark.asyncio
    async def test_jalsarraf_pinned_at_max(self, manager, freeze_time):
        state = await manager.get_state("jalsarraf")
        assert state.score == 1000
        assert state.level == 9
        assert state.level_name == "Oath Fulfilled"
        # Pinned state is cached under the user id.
        assert manager._states["jalsarraf"].score == 1000

    @pytest.mark.asyncio
    async def test_other_user_loads_from_db_row(self, manager):
        row = (250, 4, "Trusted Ally", FIXED_TODAY, 3, 2, 17, FIXED_NOW)
        with _patch_db(fetchone_result=row):
            state = await manager.get_state("alice")
        assert state.score == 250
        assert state.level == 4
        assert state.consecutive_days == 3
        assert state.total_interactions == 17

    @pytest.mark.asyncio
    async def test_other_user_missing_row_is_default(self, manager):
        with _patch_db(fetchone_result=None):
            state = await manager.get_state("bob")
        assert state.score == 0
        assert state.level == 0
        assert state.level_name == "Cold Assessment"


# ── _load_state: hard-floor anomaly guard + error fallback ───────────────────


class TestLoadState:
    @pytest.mark.asyncio
    async def test_loads_row_into_cache(self, manager):
        row = (500, 6, "Deep Devotion", FIXED_TODAY, 5, 4, 99, FIXED_NOW)
        with _patch_db(fetchone_result=row):
            await manager._load_state("carol")
        st = manager._states["carol"]
        assert st.score == 500
        assert st.level == 6

    @pytest.mark.asyncio
    async def test_hard_floor_rejects_anomalous_drop(self, manager):
        # Seed a cached high score, then have the DB return a much lower one.
        manager._states["dan"] = AffectionState(score=400, level=5, level_name="Unguarded")
        # DB returns 100 (drop of 300 > 50 threshold) → keep cached 400, push UPDATE.
        with _patch_db(fetchone_result=(100, 2, "Professional Respect",
                                        FIXED_TODAY, 1, 0, 5, FIXED_NOW)) as conn:
            await manager._load_state("dan")
        # Cached high value preserved.
        assert manager._states["dan"].score == 400
        # An UPDATE write-back was issued to correct the DB.
        sqls = [c.args[0] for c in conn.execute.call_args_list]
        assert any("UPDATE companion_affection SET score" in s for s in sqls)

    @pytest.mark.asyncio
    async def test_small_drop_within_floor_is_accepted(self, manager):
        # Cached 400, DB returns 370 (drop of 30 < 50) → accept DB value.
        manager._states["eve"] = AffectionState(score=400, level=5, level_name="Unguarded")
        with _patch_db(fetchone_result=(370, 5, "Unguarded", FIXED_TODAY, 1, 0, 5, FIXED_NOW)):
            await manager._load_state("eve")
        assert manager._states["eve"].score == 370

    @pytest.mark.asyncio
    async def test_db_exception_falls_back_to_default_state(self, manager):
        @asynccontextmanager
        async def _boom(*a, **k):
            raise RuntimeError("db down")
            yield  # pragma: no cover

        with patch("app.affection.get_conn", _boom):
            await manager._load_state("frank")
        # On error with no prior cache, a default state is installed.
        assert manager._states["frank"].score == 0

    @pytest.mark.asyncio
    async def test_db_exception_keeps_existing_cache(self, manager):
        manager._states["gina"] = AffectionState(score=600, level=7, level_name="Vulnerable")

        @asynccontextmanager
        async def _boom(*a, **k):
            raise RuntimeError("db down")
            yield  # pragma: no cover

        with patch("app.affection.get_conn", _boom):
            await manager._load_state("gina")
        # Existing cache untouched on error.
        assert manager._states["gina"].score == 600


# ── _apply_delta: the core scoring engine ────────────────────────────────────


class TestApplyDelta:
    @pytest.mark.asyncio
    async def test_positive_delta_increments_score(self, manager, freeze_time):
        # Start a non-pinned user at 100, last interacted today (no streak bonus).
        manager._states["alice"] = AffectionState(
            score=100, level=2, level_name="Professional Respect",
            last_interaction_date=FIXED_TODAY, consecutive_days=1, daily_points_earned=0,
        )
        with _patch_db():
            change = await manager._apply_delta("compliment", 10, "alice")
        # compliment@10 = +5. No streak (consecutive_days == 1).
        assert change.delta == 5
        assert change.new_score == 105
        assert isinstance(change, AffectionChange)

    @pytest.mark.asyncio
    async def test_daily_cap_clamps_delta(self, manager, freeze_time):
        # Already earned 6 today; cap is 8 → only 2 more allowed even though
        # compliment@10 would be +5.
        manager._states["alice"] = AffectionState(
            score=100, level=2, last_interaction_date=FIXED_TODAY,
            consecutive_days=1, daily_points_earned=DAILY_POINTS_CAP - 2,
        )
        with _patch_db():
            change = await manager._apply_delta("compliment", 10, "alice")
        assert change.delta == 2
        assert change.new_score == 102

    @pytest.mark.asyncio
    async def test_daily_cap_exhausted_blocks_gain(self, manager, freeze_time):
        manager._states["alice"] = AffectionState(
            score=100, level=2, last_interaction_date=FIXED_TODAY,
            consecutive_days=1, daily_points_earned=DAILY_POINTS_CAP,
        )
        with _patch_db():
            change = await manager._apply_delta("compliment", 10, "alice")
        assert change.delta == 0
        assert change.new_score == 100

    @pytest.mark.asyncio
    async def test_daily_cap_is_config_driven(self, manager, freeze_time, monkeypatch):
        """The cap reads affection.scoring.daily_points_cap from config; the
        DAILY_POINTS_CAP constant is only a fallback. A lower config value must
        clamp tighter than the constant's 8."""
        import copy

        from app.personality import load_personality as _load_real

        cfg = copy.deepcopy(_load_real())
        cfg["affection"]["scoring"]["daily_points_cap"] = 3
        monkeypatch.setattr("app.affection.load_personality", lambda *a, **k: cfg)

        manager._states["alice"] = AffectionState(
            score=100, level=2, last_interaction_date=FIXED_TODAY,
            consecutive_days=1, daily_points_earned=2,
        )
        with _patch_db():
            change = await manager._apply_delta("compliment", 10, "alice")
        # compliment@10 = +5, but config cap=3 with 2 already earned → only +1.
        assert change.delta == 1
        assert change.new_score == 101

    @pytest.mark.asyncio
    async def test_jalsarraf_negative_delta_floored_to_zero(self, manager, freeze_time):
        # jalsarraf is pinned: a rude classification must not reduce score.
        with _patch_db():
            change = await manager._apply_delta("rude", 10, "jalsarraf")
        assert change.delta == 0
        # Still pinned at max.
        assert change.new_score == 1000

    @pytest.mark.asyncio
    async def test_negative_delta_reduces_other_user(self, manager, freeze_time):
        manager._states["alice"] = AffectionState(
            score=100, level=2, last_interaction_date=FIXED_TODAY, consecutive_days=1,
        )
        with _patch_db():
            change = await manager._apply_delta("rude", 10, "alice")
        # rude@10 = -8, not subject to the positive daily cap.
        assert change.delta == -8
        assert change.new_score == 92

    @pytest.mark.asyncio
    async def test_score_never_below_zero(self, manager, freeze_time):
        manager._states["alice"] = AffectionState(
            score=3, level=0, last_interaction_date=FIXED_TODAY, consecutive_days=1,
        )
        with _patch_db():
            change = await manager._apply_delta("inappropriate", 10, "alice")
        # -10 would underflow; clamps at 0.
        assert change.new_score == 0

    @pytest.mark.asyncio
    async def test_score_never_above_max(self, manager, freeze_time):
        manager._states["alice"] = AffectionState(
            score=MAX_SCORE - 1, level=9, last_interaction_date=FIXED_TODAY,
            consecutive_days=1, daily_points_earned=0,
        )
        with _patch_db():
            change = await manager._apply_delta("compliment", 10, "alice")
        assert change.new_score == MAX_SCORE  # clamped, not 1004

    @pytest.mark.asyncio
    async def test_level_up_sets_direction_and_publishes(self, manager, freeze_time):
        # 28 → +5 (compliment@10) = 33, crossing the level-1 threshold (30).
        manager._states["alice"] = AffectionState(
            score=28, level=0, level_name="Cold Assessment",
            last_interaction_date=FIXED_TODAY, consecutive_days=1, daily_points_earned=0,
        )
        with _patch_db(), patch("app.affection.publish_event", new=AsyncMock()) as pub:
            change = await manager._apply_delta("compliment", 10, "alice")
        assert change.new_score == 33
        assert change.level_changed is True
        assert change.level_direction == "up"
        assert change.new_level == 1
        pub.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_level_down_sets_direction(self, manager, freeze_time):
        # 32 (level 1) → rude@10 = -8 → 24 → back to level 0.
        manager._states["alice"] = AffectionState(
            score=32, level=1, level_name="Acknowledged",
            last_interaction_date=FIXED_TODAY, consecutive_days=1,
        )
        with _patch_db(), patch("app.affection.publish_event", new=AsyncMock()):
            change = await manager._apply_delta("rude", 10, "alice")
        assert change.new_score == 24
        assert change.level_changed is True
        assert change.level_direction == "down"
        assert change.new_level == 0

    @pytest.mark.asyncio
    async def test_no_level_change_keeps_direction_empty(self, manager, freeze_time):
        manager._states["alice"] = AffectionState(
            score=100, level=2, level_name="Professional Respect",
            last_interaction_date=FIXED_TODAY, consecutive_days=1, daily_points_earned=0,
        )
        with _patch_db():
            change = await manager._apply_delta("greeting", 1, "alice")
        assert change.level_changed is False
        assert change.level_direction == ""

    @pytest.mark.asyncio
    async def test_total_interactions_increments(self, manager, freeze_time):
        manager._states["alice"] = AffectionState(
            score=100, level=2, last_interaction_date=FIXED_TODAY,
            consecutive_days=1, total_interactions=4,
        )
        with _patch_db():
            await manager._apply_delta("greeting", 1, "alice")
        assert manager._states["alice"].total_interactions == 5

    @pytest.mark.asyncio
    async def test_first_interaction_set_when_none(self, manager, freeze_time):
        manager._states["alice"] = AffectionState(
            score=10, level=0, last_interaction_date=FIXED_TODAY,
            consecutive_days=1, first_interaction=None,
        )
        with _patch_db():
            await manager._apply_delta("greeting", 1, "alice")
        # datetime.now() is frozen to FIXED_NOW.
        assert manager._states["alice"].first_interaction == FIXED_NOW


class TestApplyDeltaDayRollover:
    @pytest.mark.asyncio
    async def test_consecutive_day_increment_and_bonus(self, manager, freeze_time):
        # Last interacted yesterday → streak increments to 2, and because it's a
        # fresh day the daily-consistency bonus (+3) applies on the first gain.
        yesterday = FIXED_TODAY.fromordinal(FIXED_TODAY.toordinal() - 1)
        manager._states["alice"] = AffectionState(
            score=100, level=2, last_interaction_date=yesterday,
            consecutive_days=1, daily_points_earned=5,
        )
        with _patch_db():
            change = await manager._apply_delta("greeting", 1, "alice")
        # New day resets daily_points_earned to 0, greeting=+1, +3 streak bonus = +4.
        assert manager._states["alice"].consecutive_days == 2
        assert change.delta == 4
        assert change.new_score == 104

    @pytest.mark.asyncio
    async def test_streak_resets_after_gap(self, manager, freeze_time):
        # Last interacted 5 days ago → streak resets to 1, no consistency bonus.
        long_ago = FIXED_TODAY.fromordinal(FIXED_TODAY.toordinal() - 5)
        manager._states["alice"] = AffectionState(
            score=100, level=2, last_interaction_date=long_ago,
            consecutive_days=9, daily_points_earned=8,
        )
        with _patch_db():
            change = await manager._apply_delta("greeting", 1, "alice")
        assert manager._states["alice"].consecutive_days == 1
        # No streak bonus (consecutive_days == 1), just greeting=+1.
        assert change.delta == 1
        assert change.new_score == 101

    @pytest.mark.asyncio
    async def test_first_ever_interaction_streak_is_one(self, manager, freeze_time):
        # No prior interaction date → consecutive_days becomes 1.
        manager._states["alice"] = AffectionState(
            score=0, level=0, last_interaction_date=None, consecutive_days=0,
        )
        with _patch_db():
            change = await manager._apply_delta("greeting", 1, "alice")
        assert manager._states["alice"].consecutive_days == 1
        assert change.delta == 1


# ── apply_classification / classify_and_adjust: thin wrappers ────────────────


class TestClassificationWrappers:
    @pytest.mark.asyncio
    async def test_apply_classification_delegates_to_delta(self, manager, freeze_time):
        manager._states["alice"] = AffectionState(
            score=100, level=2, last_interaction_date=FIXED_TODAY,
            consecutive_days=1, daily_points_earned=0,
        )
        with _patch_db():
            change = await manager.apply_classification("compliment", 10, "alice")
        assert change.delta == 5
        assert change.new_score == 105

    @pytest.mark.asyncio
    async def test_classify_and_adjust_uses_classifier_result(self, manager, freeze_time):
        manager._states["alice"] = AffectionState(
            score=100, level=2, last_interaction_date=FIXED_TODAY,
            consecutive_days=1, daily_points_earned=0,
        )
        with _patch_db(), patch.object(
            manager, "_classify_interaction",
            new=AsyncMock(return_value=("compliment", 10)),
        ):
            change = await manager.classify_and_adjust("you're amazing", "...thanks", "alice")
        assert change.reason == "compliment"
        assert change.delta == 5


# ── _classify_interaction: LLM call, parsing, fallback ───────────────────────


def _gate_cm():
    """Return an async-context-manager mock usable as `async with gate:`."""
    gate = MagicMock()
    gate.__aenter__ = AsyncMock(return_value=None)
    gate.__aexit__ = AsyncMock(return_value=None)
    return gate


def _llm_response(content: str):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(
        return_value={"choices": [{"message": {"content": content}}]}
    )
    return resp


class TestClassifyInteraction:
    @pytest.mark.asyncio
    async def test_parses_plain_json(self, manager):
        manager._http = MagicMock()
        manager._http.post = AsyncMock(
            return_value=_llm_response('{"type": "compliment", "intensity": 7}')
        )
        with patch("app.llm_router.get_lm_gate", return_value=_gate_cm()), patch(
            "app.llm_router.LM_TTL_SECONDS", 60, create=True
        ):
            itype, intensity = await manager._classify_interaction("you rock", "hm")
        assert itype == "compliment"
        assert intensity == 7

    @pytest.mark.asyncio
    async def test_strips_markdown_code_fence(self, manager):
        fenced = '```json\n{"type": "rude", "intensity": 9}\n```'
        manager._http = MagicMock()
        manager._http.post = AsyncMock(return_value=_llm_response(fenced))
        with patch("app.llm_router.get_lm_gate", return_value=_gate_cm()), patch(
            "app.llm_router.LM_TTL_SECONDS", 60, create=True
        ):
            itype, intensity = await manager._classify_interaction("shut up", "...")
        assert itype == "rude"
        assert intensity == 9

    @pytest.mark.asyncio
    async def test_intensity_clamped_to_range(self, manager):
        # intensity 99 must clamp to 10.
        manager._http = MagicMock()
        manager._http.post = AsyncMock(
            return_value=_llm_response('{"type": "compliment", "intensity": 99}')
        )
        with patch("app.llm_router.get_lm_gate", return_value=_gate_cm()), patch(
            "app.llm_router.LM_TTL_SECONDS", 60, create=True
        ):
            itype, intensity = await manager._classify_interaction("a", "b")
        assert intensity == 10

    @pytest.mark.asyncio
    async def test_low_intensity_clamped_up(self, manager):
        manager._http = MagicMock()
        manager._http.post = AsyncMock(
            return_value=_llm_response('{"type": "neutral", "intensity": 0}')
        )
        with patch("app.llm_router.get_lm_gate", return_value=_gate_cm()), patch(
            "app.llm_router.LM_TTL_SECONDS", 60, create=True
        ):
            _, intensity = await manager._classify_interaction("a", "b")
        assert intensity == 1

    @pytest.mark.asyncio
    async def test_malformed_json_falls_back_to_neutral(self, manager):
        manager._http = MagicMock()
        manager._http.post = AsyncMock(return_value=_llm_response("not json at all"))
        with patch("app.llm_router.get_lm_gate", return_value=_gate_cm()), patch(
            "app.llm_router.LM_TTL_SECONDS", 60, create=True
        ):
            itype, intensity = await manager._classify_interaction("a", "b")
        assert itype == "neutral"
        assert intensity == 5

    @pytest.mark.asyncio
    async def test_http_error_falls_back_to_neutral(self, manager):
        manager._http = MagicMock()
        manager._http.post = AsyncMock(side_effect=RuntimeError("connection refused"))
        with patch("app.llm_router.get_lm_gate", return_value=_gate_cm()), patch(
            "app.llm_router.LM_TTL_SECONDS", 60, create=True
        ):
            itype, intensity = await manager._classify_interaction("a", "b")
        assert itype == "neutral"
        assert intensity == 5

    @pytest.mark.asyncio
    async def test_missing_type_defaults_to_neutral(self, manager):
        # Valid JSON but no "type" key → defaults to "neutral".
        manager._http = MagicMock()
        manager._http.post = AsyncMock(
            return_value=_llm_response('{"intensity": 4}')
        )
        with patch("app.llm_router.get_lm_gate", return_value=_gate_cm()), patch(
            "app.llm_router.LM_TTL_SECONDS", 60, create=True
        ):
            itype, intensity = await manager._classify_interaction("a", "b")
        assert itype == "neutral"
        assert intensity == 4


# ── _apply_absence_decay: time-based score decay ─────────────────────────────


class TestAbsenceDecay:
    @pytest.mark.asyncio
    async def test_jalsarraf_is_exempt(self, manager, freeze_time):
        # Should return immediately without touching DB.
        with patch.object(manager, "get_state", new=AsyncMock()) as gs:
            await manager._apply_absence_decay("jalsarraf")
        gs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_decay_when_never_interacted(self, manager, freeze_time):
        manager._states["alice"] = AffectionState(score=100, last_interaction_date=None)
        with _patch_db(), patch.object(manager, "_save_state", new=AsyncMock()) as save:
            await manager._apply_absence_decay("alice")
        save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_decay_within_one_day(self, manager, freeze_time):
        manager._states["alice"] = AffectionState(
            score=100, last_interaction_date=FIXED_TODAY,
        )
        with _patch_db(), patch.object(manager, "_save_state", new=AsyncMock()) as save:
            await manager._apply_absence_decay("alice")
        # days_absent == 0 → no decay applied.
        save.assert_not_awaited()
        assert manager._states["alice"].score == 100

    @pytest.mark.asyncio
    async def test_decay_applied_after_multi_day_absence(self, manager, freeze_time):
        # Last interaction 4 days ago → 3 "absent" days × -2 = -6.
        four_days_ago = FIXED_TODAY.fromordinal(FIXED_TODAY.toordinal() - 4)
        manager._states["alice"] = AffectionState(
            score=200, level=3, level_name="Guarded Interest",
            last_interaction_date=four_days_ago,
        )
        with _patch_db(), patch.object(manager, "_save_state", new=AsyncMock()):
            await manager._apply_absence_decay("alice")
        # 3 days × -2 = -6; 10% of 200 = 20 and 30 → cap is min(20,30)=20, so -6 applies.
        assert manager._states["alice"].score == 194

    @pytest.mark.asyncio
    async def test_decay_capped_at_ten_percent(self, manager, freeze_time):
        # Long absence: 100 days ago × -2 = -198 raw, but cap = min(10% of score, 30).
        long_ago = FIXED_TODAY.fromordinal(FIXED_TODAY.toordinal() - 100)
        manager._states["alice"] = AffectionState(
            score=150, level=3, level_name="Guarded Interest",
            last_interaction_date=long_ago,
        )
        with _patch_db(), patch.object(manager, "_save_state", new=AsyncMock()):
            await manager._apply_absence_decay("alice")
        # 10% of 150 = 15, min(15, 30) = 15 → score 150 - 15 = 135.
        assert manager._states["alice"].score == 135

    @pytest.mark.asyncio
    async def test_decay_cap_uses_thirty_when_ten_percent_larger(self, manager, freeze_time):
        # High score: 10% would be 80 but the 30-point hard cap wins.
        long_ago = FIXED_TODAY.fromordinal(FIXED_TODAY.toordinal() - 100)
        manager._states["alice"] = AffectionState(
            score=800, level=8, level_name="Bonded",
            last_interaction_date=long_ago,
        )
        with _patch_db(), patch.object(manager, "_save_state", new=AsyncMock()):
            await manager._apply_absence_decay("alice")
        # min(80, 30) = 30 → 800 - 30 = 770.
        assert manager._states["alice"].score == 770

    @pytest.mark.asyncio
    async def test_zero_decay_rate_is_a_noop(self, manager, freeze_time):
        # Admin may disable decay by setting absence_decay_per_day: 0 → with a
        # multi-day absence the computed total is 0, so the function returns
        # early without persisting or changing the score.
        five_days_ago = FIXED_TODAY.fromordinal(FIXED_TODAY.toordinal() - 5)
        manager._states["alice"] = AffectionState(
            score=200, level=3, level_name="Guarded Interest",
            last_interaction_date=five_days_ago,
        )
        cfg = {"affection": {"scoring": {"absence_decay_per_day": 0}}}
        with _patch_db(), patch(
            "app.affection.load_personality", return_value=cfg
        ), patch.object(manager, "_save_state", new=AsyncMock()) as save:
            await manager._apply_absence_decay("alice")
        save.assert_not_awaited()
        assert manager._states["alice"].score == 200

    @pytest.mark.asyncio
    async def test_decay_logs_change(self, manager, freeze_time):
        three_days_ago = FIXED_TODAY.fromordinal(FIXED_TODAY.toordinal() - 3)
        manager._states["alice"] = AffectionState(
            score=200, level=3, level_name="Guarded Interest",
            last_interaction_date=three_days_ago,
        )
        with _patch_db(), patch.object(
            manager, "_save_state", new=AsyncMock()
        ), patch.object(manager, "_log_change", new=AsyncMock()) as log:
            await manager._apply_absence_decay("alice")
        # 2 absent days × -2 = -4 → logged.
        log.assert_awaited_once()
        logged_delta = log.await_args.args[0]
        assert logged_delta == -4


# ── _save_state / _log_change: persistence SQL ───────────────────────────────


class TestPersistence:
    @pytest.mark.asyncio
    async def test_save_state_issues_update_and_commit(self, manager):
        state = AffectionState(score=321, level=4, level_name="Trusted Ally")
        with _patch_db() as conn:
            await manager._save_state(state, "alice")
        conn.execute.assert_awaited_once()
        sql = conn.execute.await_args.args[0]
        params = conn.execute.await_args.args[1]
        assert "UPDATE companion_affection SET" in sql
        assert params[0] == 321  # score is the first bound param
        assert params[-1] == "alice"  # user_id is the last
        conn.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_save_state_swallows_db_error(self, manager):
        state = AffectionState(score=10)

        @asynccontextmanager
        async def _boom(*a, **k):
            raise RuntimeError("write failed")
            yield  # pragma: no cover

        with patch("app.affection.get_conn_autocommit", _boom):
            # Must not raise — persistence failures are logged, not propagated.
            await manager._save_state(state, "alice")

    @pytest.mark.asyncio
    async def test_log_change_inserts_row(self, manager):
        with _patch_db() as conn:
            await manager._log_change(-8, "rude", 100, 92, 2, 2, "alice")
        conn.execute.assert_awaited_once()
        sql = conn.execute.await_args.args[0]
        params = conn.execute.await_args.args[1]
        assert "INSERT INTO companion_affection_log" in sql
        assert params == (-8, "rude", 100, 92, 2, 2, "alice")
        conn.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_log_change_swallows_db_error(self, manager):
        @asynccontextmanager
        async def _boom(*a, **k):
            raise RuntimeError("insert failed")
            yield  # pragma: no cover

        with patch("app.affection.get_conn_autocommit", _boom):
            await manager._log_change(1, "greeting", 0, 1, 0, 0, "alice")


# ── init / close lifecycle ───────────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_init_loads_levels_and_runs_decay(self, manager, freeze_time):
        # init() creates an httpx client, loads levels from personality, then
        # runs _load_state + _apply_absence_decay for jalsarraf (the decay is a
        # no-op for jalsarraf, but _load_state still runs).
        with _patch_db(fetchone_result=None):
            await manager.init()
        assert manager._http is not None
        assert len(manager._levels) >= 1
        # personality.yaml level 0 is Cold Assessment.
        assert manager._levels[0]["name"] == "Cold Assessment"
        await manager.close()

    @pytest.mark.asyncio
    async def test_close_is_safe_when_http_none(self, manager):
        manager._http = None
        # Should not raise.
        await manager.close()

    @pytest.mark.asyncio
    async def test_close_closes_http_client(self, manager):
        fake_http = MagicMock()
        fake_http.aclose = AsyncMock()
        manager._http = fake_http
        await manager.close()
        fake_http.aclose.assert_awaited_once()


# ── _state property (legacy jalsarraf accessor) ──────────────────────────────


class TestStateProperty:
    def test_state_getter_returns_jalsarraf_cache(self, manager):
        assert manager._state is None  # nothing cached yet
        st = AffectionState(score=42)
        manager._states["jalsarraf"] = st
        assert manager._state is st

    def test_state_setter_stores_under_jalsarraf(self, manager):
        st = AffectionState(score=99)
        manager._state = st
        assert manager._states["jalsarraf"] is st

    def test_state_setter_ignores_none(self, manager):
        manager._states["jalsarraf"] = AffectionState(score=7)
        manager._state = None  # no-op per implementation
        assert manager._states["jalsarraf"].score == 7


# ── add_score: flat reward path (gifts, missions) ────────────────────────────


class TestAddScore:
    """add_score clamps to [0, MAX_SCORE] and always recomputes the level.

    Regression guard for the mission reward that capped at 100 (truncating any
    score above 100) and for reward handlers that bumped score without
    recomputing the level (shipping a stale level to the client).
    """

    async def test_does_not_truncate_score_above_100(self, manager):
        # The bug: min(100, score + 3) slammed any score > 100 down to 100.
        manager._states["alice"] = AffectionState(
            score=500, level=5, level_name="Trusted Operator"
        )
        manager._save_state = AsyncMock()
        with patch.object(manager, "_load_state", new=AsyncMock()):
            state = await manager.add_score(3, "alice")
        assert state.score == 503
        manager._save_state.assert_awaited_once()

    async def test_clamps_at_max_score(self, manager):
        manager._states["alice"] = AffectionState(
            score=990, level=9, level_name="Oath Fulfilled"
        )
        manager._save_state = AsyncMock()
        with patch.object(manager, "_load_state", new=AsyncMock()):
            state = await manager.add_score(50, "alice")
        assert state.score == MAX_SCORE

    async def test_recomputes_level_on_promotion(self, manager):
        # 79 -> level 1; +1 crosses the 80 threshold -> level 2.
        manager._states["alice"] = AffectionState(
            score=79, level=1, level_name="Acknowledged"
        )
        manager._save_state = AsyncMock()
        with patch.object(manager, "_load_state", new=AsyncMock()):
            state = await manager.add_score(1, "alice")
        assert state.score == 80
        assert (state.level, state.level_name) == (2, "Professional Respect")

    async def test_never_goes_negative(self, manager):
        manager._states["alice"] = AffectionState(
            score=2, level=0, level_name="Cold Assessment"
        )
        manager._save_state = AsyncMock()
        with patch.object(manager, "_load_state", new=AsyncMock()):
            state = await manager.add_score(-50, "alice")
        assert state.score == 0

    async def test_jalsarraf_negative_adjustment_ignored(self, manager):
        # Commander is pinned at max trust — affection is SACRED, never reduced.
        manager._save_state = AsyncMock()
        state = await manager.add_score(-100, "jalsarraf")
        assert state.score == MAX_SCORE
        assert state.level == 9


# ── Data-loss fixes (2026-06-11): anomaly-restore commit + real counters ─────


class TestAnomalyRestoreCommit:
    @pytest.mark.asyncio
    async def test_anomaly_writeback_is_committed(self, manager):
        """The hard-floor restore UPDATE runs on a manual-commit connection —
        without an explicit commit it silently rolls back and the restored
        score never reaches the DB."""
        manager._states["dan"] = AffectionState(score=400, level=5, level_name="Unguarded")
        with _patch_db(fetchone_result=(100, 2, "Professional Respect",
                                        FIXED_TODAY, 1, 0, 5, FIXED_NOW)) as conn:
            await manager._load_state("dan")
        sqls = [c.args[0] for c in conn.execute.call_args_list]
        assert any("UPDATE companion_affection SET score" in s for s in sqls)
        conn.commit.assert_awaited()


class TestJalsarrafRealCounters:
    """get_state must pin ONLY score/level/level_name (SACRED) for jalsarraf.

    The interaction counters (total_interactions, consecutive_days,
    first_interaction, daily cap, streak date) must come from the DB row —
    the old hardcoded 338/7/2026-04-06 snapshot was persisted back on every
    message, permanently clobbering the real counters.
    """

    @pytest.mark.asyncio
    async def test_pins_score_but_loads_real_counters_from_db(self, manager):
        row = (1000, 9, "Oath Fulfilled", FIXED_TODAY, 3, 2, 42, FIXED_NOW)
        with _patch_db(fetchone_result=row):
            state = await manager.get_state("jalsarraf")
        # SACRED pin intact.
        assert state.score == 1000
        assert state.level == 9
        assert state.level_name == "Oath Fulfilled"
        # Real counters from the DB row — NOT the old hardcoded snapshot.
        assert state.total_interactions == 42
        assert state.consecutive_days == 3
        assert state.first_interaction == FIXED_NOW
        assert state.last_interaction_date == FIXED_TODAY
        assert state.daily_points_earned == 2

    @pytest.mark.asyncio
    async def test_low_db_score_still_pinned_to_max(self, manager):
        # Even if the DB row drifted low, the SACRED pin holds.
        row = (120, 3, "Guarded Interest", FIXED_TODAY, 1, 0, 7, FIXED_NOW)
        with _patch_db(fetchone_result=row):
            state = await manager.get_state("jalsarraf")
        assert (state.score, state.level, state.level_name) == (1000, 9, "Oath Fulfilled")
        assert state.total_interactions == 7  # counter still real

    @pytest.mark.asyncio
    async def test_apply_delta_increments_real_counter(self, manager, freeze_time):
        """A message must increment the REAL total_interactions (42→43), not
        re-persist the hardcoded 338."""
        row = (1000, 9, "Oath Fulfilled", FIXED_TODAY, 3, 0, 42, FIXED_NOW)
        with _patch_db(fetchone_result=row), \
             patch.object(manager, "_save_state", new=AsyncMock()) as save:
            await manager._apply_delta("greeting", 2, "jalsarraf")
        saved_state = save.await_args.args[0]
        assert saved_state.total_interactions == 43
        assert saved_state.total_interactions != 338
        assert saved_state.score == 1000  # pin survives the delta
