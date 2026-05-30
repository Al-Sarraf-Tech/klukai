"""Additional pure-function tests to push coverage toward S+ rubric 95%.

S+ Phase 4 §5.5. Tests target uncovered pure-function code in the lowest-cov
modules. No DB / network / FastAPI / file I/O.
"""

from __future__ import annotations

import asyncio

import pytest


# ── AffectionState pydantic model ─────────────────────────────────────────────
class TestAffectionState:
    def test_defaults(self) -> None:
        from app.affection import AffectionState
        s = AffectionState()
        assert s.score == 0
        assert s.level == 0
        assert s.level_name == "Cold Assessment"

    def test_custom_values(self) -> None:
        from app.affection import AffectionState
        s = AffectionState(score=500, level=5, level_name="Admitted Bond")
        assert s.score == 500
        assert s.level == 5
        assert s.level_name == "Admitted Bond"

    def test_level_bounds_valid(self) -> None:
        from app.affection import AffectionState
        for lvl in range(0, 10):
            s = AffectionState(score=lvl * 100, level=lvl)
            # The model preserves the exact level + score it was constructed with.
            assert s.level == lvl
            assert s.score == lvl * 100


# ── llm_json parsers ──────────────────────────────────────────────────────────
class TestLlmJsonExtraction:
    def test_extract_text_choices_message_content(self) -> None:
        from app.llm_json import extract_text
        resp = {"choices": [{"message": {"content": "hello world"}}]}
        assert extract_text(resp) == "hello world"

    def test_extract_text_empty_dict(self) -> None:
        from app.llm_json import extract_text
        # No "choices" key → empty string (the documented default).
        assert extract_text({}) == ""

    def test_extract_text_empty_choices(self) -> None:
        from app.llm_json import extract_text
        # Empty choices list → empty string, never raises.
        assert extract_text({"choices": []}) == ""

    def test_extract_text_nested_message(self) -> None:
        from app.llm_json import extract_text
        resp = {"choices": [{"message": {"content": "test", "role": "assistant"}}]}
        assert extract_text(resp) == "test"

    def test_parse_json_dict(self) -> None:
        from app.llm_json import parse_json
        assert parse_json('{"a": 1}') == {"a": 1}

    def test_parse_json_array(self) -> None:
        from app.llm_json import parse_json
        # Top-level JSON arrays parse to the exact list.
        assert parse_json("[1, 2, 3]") == [1, 2, 3]

    def test_parse_json_bool(self) -> None:
        from app.llm_json import parse_json
        assert parse_json("true") is True
        assert parse_json("false") is False

    def test_parse_json_null(self) -> None:
        from app.llm_json import parse_json
        assert parse_json("null") is None

    def test_parse_json_int(self) -> None:
        from app.llm_json import parse_json
        # Bare integer literal parses to the int 42.
        assert parse_json("42") == 42

    def test_parse_json_invalid(self) -> None:
        from app.llm_json import parse_json
        assert parse_json("not json") is None

    def test_parse_json_empty(self) -> None:
        from app.llm_json import parse_json
        assert parse_json("") is None

    def test_parse_json_extracts_from_text(self) -> None:
        from app.llm_json import parse_json
        # The _JSON_OBJECT regex extracts the embedded object out of prose.
        result = parse_json('Sure! Here is: {"key": "value"} as requested.')
        assert result == {"key": "value"}

    def test_parse_json_array_brackets(self) -> None:
        from app.llm_json import parse_json
        # Top-level array of objects parses to the exact list of dicts.
        assert parse_json('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]


# ── helpers detectors ────────────────────────────────────────────────────────
class TestHelperDetectors:
    def test_detect_jealousy_trigger(self) -> None:
        from app.helpers import detect_jealousy_trigger
        # A squad name + a compliment fires and returns the canonical name.
        assert detect_jealousy_trigger("Mechty is amazing") == "Mechty"
        # No squad member named → no jealousy, even with "ex"/"another woman".
        assert detect_jealousy_trigger("My ex called me") is None
        assert detect_jealousy_trigger("Another woman") is None
        assert detect_jealousy_trigger("hello") is None

    def test_detect_gift_giving(self) -> None:
        from app.helpers import detect_gift_giving
        # "bought you X" matches the gifting pattern; greetings do not.
        assert detect_gift_giving("I bought you flowers") is True
        assert detect_gift_giving("hello") is False

    def test_detect_commander_details(self) -> None:
        from app.helpers import detect_commander_details
        # "My birthday is in June" matches NO detail category (birthday is not
        # a tracked category) → empty dict, NOT a populated one.
        assert detect_commander_details("My birthday is in June") == {}
        # A wearing phrase produces exactly the 'wearing' flag.
        assert detect_commander_details("I'm wearing a coat") == {"wearing": True}

    def test_wants_dream_inquiry(self) -> None:
        from app.helpers import wants_dream_inquiry
        assert wants_dream_inquiry("did you dream?") is True
        assert wants_dream_inquiry("hello") is False

    def test_wants_recall(self) -> None:
        from app.helpers import wants_recall
        assert wants_recall("remember when?") is True
        assert wants_recall("hello") is False

    def test_wants_mission_start(self) -> None:
        from app.helpers import wants_mission_start
        # Real trigger phrases come from MISSION_START_KEYWORDS — note that a
        # bare "start a mission" is NOT a trigger; "keep me posted" is.
        assert wants_mission_start("keep me posted") is True
        assert wants_mission_start("updates every 30 min") is True
        assert wants_mission_start("start a mission") is False
        assert wants_mission_start("hello") is False

    def test_wants_mission_cancel(self) -> None:
        from app.helpers import wants_mission_cancel
        # "cancel mission" is NOT a keyword; "stop updates"/"stand down" are.
        assert wants_mission_cancel("stop updates") is True
        assert wants_mission_cancel("stand down") is True
        assert wants_mission_cancel("cancel mission") is False
        assert wants_mission_cancel("hello") is False

    def test_parse_interval_minutes_default(self) -> None:
        from app.helpers import parse_interval_minutes
        # No "every N" pattern → the 30-minute default.
        assert parse_interval_minutes("hello") == 30

    def test_parse_interval_minutes_explicit(self) -> None:
        from app.helpers import parse_interval_minutes
        # Explicit minute and hour patterns parse to exact values.
        assert parse_interval_minutes("every 45 minutes") == 45
        assert parse_interval_minutes("every 2 hours") == 120
        # Below the 5-minute floor clamps up to 5.
        assert parse_interval_minutes("every 3 min") == 5

    def test_fix_narration_empty(self) -> None:
        from app.helpers import fix_narration
        assert fix_narration("") == ""

    def test_fix_narration_normal(self) -> None:
        from app.helpers import fix_narration
        # First-person, artifact-free text passes through unchanged.
        assert fix_narration("She said hello.") == "She said hello."

    def test_chunk_text_empty(self) -> None:
        from app.helpers import chunk_text
        assert chunk_text("") == []

    def test_chunk_text_short(self) -> None:
        from app.helpers import chunk_text
        # Default chunk size is 8 → "hello world" (11 chars) splits into 2.
        assert chunk_text("hello world") == ["hello wo", "rld"]


# ── llm_router pure helpers ───────────────────────────────────────────────────
class TestLlmRouterHelpers:
    def test_lm_gate_is_shared_singleton(self) -> None:
        from app import llm_router
        # get_lm_gate returns a real asyncio.Lock and the SAME instance each call
        # (single shared gate across all LM Studio callers).
        gate = llm_router.get_lm_gate()
        assert isinstance(gate, asyncio.Lock)
        assert llm_router.get_lm_gate() is gate
        # An unlocked gate is not busy.
        assert llm_router.lm_gate_busy() is False

    def test_local_tools_aliases_agent_model(self) -> None:
        from app import llm_router
        # LOCAL_TOOLS is defined as an alias of LOCAL_AGENT — they must be equal.
        assert llm_router.LOCAL_TOOLS == llm_router.LOCAL_AGENT

    def test_session_state_construction(self) -> None:
        from app.models import SessionState
        s = SessionState(conversation_id="test-conv-id")
        assert s.conversation_id == "test-conv-id"


# ── memory pure helpers ───────────────────────────────────────────────────────
class TestMemoryPure:
    def test_is_zero_vector_all_zeros(self) -> None:
        from app.memory import MemoryManager
        assert MemoryManager.is_zero_vector([0.0, 0.0, 0.0]) is True

    def test_is_zero_vector_non_zero(self) -> None:
        from app.memory import MemoryManager
        assert MemoryManager.is_zero_vector([0.0, 0.1, 0.0]) is False

    def test_is_zero_vector_empty(self) -> None:
        from app.memory import MemoryManager
        # all([]) is True → an empty vector is treated as a zero vector.
        assert MemoryManager.is_zero_vector([]) is True


# ── physical_state coverage ───────────────────────────────────────────────────
class TestPhysicalState:
    def test_normal_state_has_empty_description(self) -> None:
        from app import physical_state
        # 'normal' is the resting state and renders no prompt text.
        assert physical_state.get_description("normal") == ""
        assert physical_state.STATES["normal"]["decay_hours"] is None

    def test_sore_state_description_and_decay(self) -> None:
        from app import physical_state
        # Concrete copy + decay window for the 'sore' state.
        assert physical_state.get_description("sore") == "muscles ache from recent combat"
        assert physical_state.STATES["sore"]["decay_hours"] == 4


# ── billing pure ──────────────────────────────────────────────────────────────
class TestBillingPure:
    def test_subscription_construction(self) -> None:
        from app.billing import Subscription
        s = Subscription(user_id="test", tier="free", status="active")
        assert s.user_id == "test"
        assert s.tier == "free"

    def test_quota_exceeded_exception(self) -> None:
        from app.billing import QuotaExceeded
        # Signature is (counter, tier, limit); it stores them and builds a
        # human-readable message.
        e = QuotaExceeded("chat_messages_per_day", "free", 50)
        assert isinstance(e, Exception)
        assert e.counter == "chat_messages_per_day"
        assert e.tier == "free"
        assert e.limit == 50
        assert str(e) == "Quota exceeded: chat_messages_per_day (tier=free, limit=50/period)"


# ── caches ────────────────────────────────────────────────────────────────────
class TestCaches:
    def test_cosine_identical_and_orthogonal(self) -> None:
        from app import caches
        # Identical unit vectors → 1.0; orthogonal → 0.0.
        assert caches.cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
        assert caches.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_cosine_zero_and_mismatched_return_zero(self) -> None:
        from app import caches
        # Zero-magnitude or length-mismatched inputs fail safe to 0.0.
        assert caches.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
        assert caches.cosine([1.0], [1.0, 0.0]) == 0.0

    def test_text_key_is_namespaced_and_stable(self) -> None:
        from app import caches
        # Embedding cache keys are namespaced and deterministic per text.
        assert caches._text_key("hello").startswith("cache:embed:")
        assert caches._text_key("hello") == caches._text_key("hello")
        assert caches._text_key("hello") != caches._text_key("world")


# ── circuit_breakers extended ─────────────────────────────────────────────────
class TestCircuitBreakerEdges:
    @pytest.mark.asyncio
    async def test_breaker_returns_value(self) -> None:
        from app.circuit_breakers import BreakerConfig, CircuitBreaker

        cb = CircuitBreaker(cfg=BreakerConfig(name="value-test"))

        async def returner() -> dict:
            return {"key": "value", "num": 42}

        result = await cb.call(returner)
        assert result == {"key": "value", "num": 42}

    @pytest.mark.asyncio
    async def test_breaker_passes_args_kwargs(self) -> None:
        from app.circuit_breakers import BreakerConfig, CircuitBreaker

        cb = CircuitBreaker(cfg=BreakerConfig(name="args-test"))

        async def fn(a: int, b: int, *, c: int = 0) -> int:
            return a + b + c

        assert await cb.call(fn, 1, 2, c=3) == 6

    @pytest.mark.asyncio
    async def test_breaker_window_pruning_no_trip(self) -> None:
        """Errors outside window are pruned; single late error doesn't trip."""
        from app.circuit_breakers import BreakerConfig, CircuitBreaker, State

        cb = CircuitBreaker(cfg=BreakerConfig(name="prune-test-2", error_threshold=3, window_seconds=0.05))

        async def boom() -> int:
            raise RuntimeError("err")

        async def ok() -> int:
            return 1

        with pytest.raises(RuntimeError):
            await cb.call(boom)
        await asyncio.sleep(0.1)
        # Pruning should have happened; first success closes the chain.
        result = await cb.call(ok)
        assert result == 1
        assert cb.state == State.CLOSED


# ── tributes coverage ─────────────────────────────────────────────────────────
class TestTributesPure:
    def test_can_send_when_none_recent(self) -> None:
        from app import tributes
        # No recent tributes → allowed, no block reason.
        assert tributes.can_send_tribute(0) == (True, None)

    def test_blocked_during_cooldown(self) -> None:
        from app import tributes
        # One in the window → blocked with the sacred-cooldown reason.
        allowed, reason = tributes.can_send_tribute(1)
        assert allowed is False
        assert reason is not None
        assert str(tributes.TRIBUTE_COOLDOWN_HOURS) in reason
