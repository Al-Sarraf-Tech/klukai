"""S-tier hardening tests: security, Pydantic validation, per-user proactive,
atomic transactions, subsystem health, episode dual-write."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# 1. Pydantic Request Models
# ═══════════════════════════════════════════════════════════════════════════

class TestPydanticModels:
    """Verify Pydantic models enforce constraints."""

    def test_login_requires_both_fields(self):
        from app.routes import LoginRequest
        with pytest.raises(Exception):
            LoginRequest()  # Missing username and password

    def test_login_accepts_valid(self):
        from app.routes import LoginRequest
        r = LoginRequest(username="test", password="pass")
        assert r.username == "test"
        assert r.password == "pass"

    def test_tts_requires_text(self):
        from app.routes import TTSRequest
        with pytest.raises(Exception):
            TTSRequest(text="")  # min_length=1

    def test_tts_default_language(self):
        from app.routes import TTSRequest
        r = TTSRequest(text="hello")
        assert r.language == "en"

    def test_stt_requires_audio(self):
        from app.routes import STTRequest
        with pytest.raises(Exception):
            STTRequest(audio="")

    def test_image_gen_max_length(self):
        from app.routes import ImageGenRequest
        with pytest.raises(Exception):
            ImageGenRequest(prompt="x" * 2001)

    def test_image_gen_min_length(self):
        from app.routes import ImageGenRequest
        with pytest.raises(Exception):
            ImageGenRequest(prompt="")

    def test_gift_requires_name(self):
        from app.routes import GiftRequest
        with pytest.raises(Exception):
            GiftRequest(gift="")

    def test_costume_accepts_any_string(self):
        from app.routes import CostumeRequest
        r = CostumeRequest(costume="blazing_star")
        assert r.costume == "blazing_star"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Per-User Proactive State
# ═══════════════════════════════════════════════════════════════════════════

class TestPerUserProactive:
    """Verify proactive engine tracks state per-user."""

    def test_per_user_affection_levels(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        engine.set_affection_level(5, user_id="alice")
        engine.set_affection_level(8, user_id="bob")
        assert engine._affection_levels["alice"] == 5
        assert engine._affection_levels["bob"] == 8

    def test_per_user_moods(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        engine.set_last_mood("tender", user_id="alice")
        engine.set_last_mood("battle_ready", user_id="bob")
        assert engine._moods["alice"] == "tender"
        assert engine._moods["bob"] == "battle_ready"

    def test_per_user_messaged_today(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        engine.mark_user_messaged_today(user_id="alice")
        assert engine._user_messaged.get("alice") is True
        assert engine._user_messaged.get("bob") is None

    def test_per_user_mission_timer_stores_description(self):
        """Mission timers are stored per-user in the dict."""
        from app.proactive import ProactiveEngine, MissionTimer
        engine = ProactiveEngine()
        timer = MissionTimer()
        timer.mission_description = "patrol east"
        engine._mission_timers["alice"] = timer
        assert "alice" in engine._mission_timers
        assert engine._mission_timers["alice"].mission_description == "patrol east"

    def test_per_user_mission_timers_independent(self):
        from app.proactive import ProactiveEngine, MissionTimer
        engine = ProactiveEngine()
        t1 = MissionTimer()
        t1.mission_description = "patrol east"
        t2 = MissionTimer()
        t2.mission_description = "patrol west"
        engine._mission_timers["alice"] = t1
        engine._mission_timers["bob"] = t2
        assert len(engine._mission_timers) == 2
        assert engine._mission_timers["alice"].mission_description == "patrol east"
        assert engine._mission_timers["bob"].mission_description == "patrol west"

    @pytest.mark.asyncio
    async def test_daily_reset_clears_per_user(self):
        from app.proactive import ProactiveEngine
        engine = ProactiveEngine()
        engine._proactive_counts["alice"] = 10
        engine._random_event_counts["bob"] = 3
        engine._romance_delivered["alice"] = True
        engine._dream_delivered["bob"] = True
        engine._user_messaged["alice"] = True
        await engine._reset_daily()
        assert len(engine._proactive_counts) == 0
        assert len(engine._random_event_counts) == 0
        assert len(engine._romance_delivered) == 0
        assert len(engine._dream_delivered) == 0
        assert len(engine._user_messaged) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 3. Agent Recall Fix Verification
# ═══════════════════════════════════════════════════════════════════════════

class TestAgentRecallFix:
    """Verify the agent recall tool uses correct imports and methods."""

    def test_recall_imports_from_context_not_companion_memory(self):
        """The fatal bug was importing nonexistent CompanionMemory."""
        import ast
        source = Path(__file__).resolve().parent.parent / "app" / "agent_loop.py"
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "memory" in node.module:
                    names = [alias.name for alias in node.names]
                    assert "CompanionMemory" not in names, \
                        "CompanionMemory import found — this class doesn't exist"

    def test_recall_uses_recall_episodes_not_search_episodic(self):
        """search_episodic doesn't exist — must use recall_episodes."""
        source = Path(__file__).resolve().parent.parent / "app" / "agent_loop.py"
        text = source.read_text()
        assert "search_episodic" not in text, "search_episodic doesn't exist on MemoryManager"
        assert "recall_episodes" in text, "Should use recall_episodes"

    def test_recall_uses_sql_like_pattern(self):
        """Data API uses SQL LIKE (%), not glob (*)."""
        source = Path(__file__).resolve().parent.parent / "app" / "agent_loop.py"
        text = source.read_text()
        # Should NOT have glob patterns in the recall function
        # The function builds pattern with % not *
        recall_func = text[text.index("_builtin_recall_memory"):text.index("_builtin_get_time")]
        assert "%" in recall_func, "Should use SQL LIKE % wildcards"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Security Headers and Middleware
# ═══════════════════════════════════════════════════════════════════════════

class TestSecurityConfig:
    """Verify security configuration at the code level."""

    def test_cors_middleware_configured(self):
        source = Path(__file__).resolve().parent.parent / "app" / "main.py"
        text = source.read_text()
        assert "CORSMiddleware" in text
        assert "klukai.appnest.cc" in text

    def test_security_headers_middleware_configured(self):
        source = Path(__file__).resolve().parent.parent / "app" / "main.py"
        text = source.read_text()
        assert "X-Content-Type-Options" in text
        assert "X-Frame-Options" in text
        assert "nosniff" in text
        assert "DENY" in text

    def test_global_exception_handler_configured(self):
        source = Path(__file__).resolve().parent.parent / "app" / "main.py"
        text = source.read_text()
        assert "exception_handler" in text
        assert "Internal server error" in text

    def test_session_cleanup_hourly(self):
        source = Path(__file__).resolve().parent.parent / "app" / "main.py"
        text = source.read_text()
        assert "3600" in text, "Session cleanup should be 1 hour (3600s)"
        assert "6 * 3600" not in text, "Old 6h cleanup should be gone"

    def test_dockerfile_non_root(self):
        dockerfile = Path(__file__).resolve().parent.parent / "Dockerfile"
        if not dockerfile.exists():
            import pytest
            pytest.skip("Dockerfile not shipped into runtime image")
        text = dockerfile.read_text()
        assert "USER appuser" in text
        assert "useradd" in text

    def test_costume_endpoints_require_auth(self):
        source = Path(__file__).resolve().parent.parent / "app" / "routes_extras.py"
        text = source.read_text()
        # Find both costume handlers — both should have _get_user_id
        costume_section = text[text.index("api_get_costume"):text.index("api_stt")]
        assert costume_section.count("_get_user_id") >= 2, \
            "Both costume GET and POST must require auth"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Atomic Transaction for store_message
# ═══════════════════════════════════════════════════════════════════════════

class TestAtomicTransaction:
    """Verify store_message uses atomic transaction."""

    def test_store_message_uses_commit(self):
        source = Path(__file__).resolve().parent.parent / "app" / "helpers.py"
        text = source.read_text()
        # Find the store_message function — it's the last function in the file
        func_start = text.index("async def store_message")
        func_text = text[func_start:]
        assert "await conn.commit()" in func_text, "Must explicitly commit for atomicity"
        assert "get_conn()" in func_text, "Must use get_conn (manual commit) not get_conn_autocommit"


# ═══════════════════════════════════════════════════════════════════════════
# 6. Episode Dual-Write
# ═══════════════════════════════════════════════════════════════════════════

class TestEpisodeDualWrite:
    """Verify episodes are written to both Qdrant and PostgreSQL."""

    def test_store_episode_writes_to_db(self):
        source = Path(__file__).resolve().parent.parent / "app" / "memory.py"
        text = source.read_text()
        func_start = text.index("async def store_episode")
        func_end = text.index("\n    async def ", func_start + 100)
        func_text = text[func_start:func_end]
        assert "companion_episodes" in func_text, "Must write to companion_episodes DB table"
        assert "QDRANT_URL" in func_text or "COLLECTION_NAME" in func_text, "Must write to Qdrant"
        assert "get_conn_autocommit" in func_text, "Must use DB connection for PostgreSQL write"


# ═══════════════════════════════════════════════════════════════════════════
# 7. Subsystem Health Endpoint
# ═══════════════════════════════════════════════════════════════════════════

class TestSubsystemHealth:
    """Verify subsystem health endpoint exists and checks all services."""

    def test_subsystem_health_endpoint_exists(self):
        source = Path(__file__).resolve().parent.parent / "app" / "routes.py"
        text = source.read_text()
        assert "/api/health/subsystems" in text

    def test_checks_all_seven_subsystems(self):
        source = Path(__file__).resolve().parent.parent / "app" / "routes.py"
        text = source.read_text()
        # Find the subsystem_health function
        func_start = text.index("subsystem_health")
        func_end = text.index("# ── Push subscription", func_start)
        func_text = text[func_start:func_end]
        for subsystem in ["database", "redis", "qdrant", "lm_studio", "comfyui", "embeddings", "voice"]:
            assert subsystem in func_text, f"Must check {subsystem} subsystem"

    def test_subsystem_health_requires_auth(self):
        source = Path(__file__).resolve().parent.parent / "app" / "routes.py"
        text = source.read_text()
        func_start = text.index("subsystem_health")
        func_end = text.index("# ── Push subscription", func_start)
        func_text = text[func_start:func_end]
        assert "_get_user_id" in func_text, "Subsystem health must require authentication"


# ═══════════════════════════════════════════════════════════════════════════
# 8. ComfyUI Port Fix
# ═══════════════════════════════════════════════════════════════════════════

class TestComfyUIPort:
    """Verify ComfyUI uses correct port."""

    def test_docker_compose_uses_8388(self):
        compose = Path(__file__).resolve().parent.parent.parent.parent / "docker-compose.yml"
        if not compose.exists():
            import pytest
            pytest.skip("docker-compose.yml not shipped into runtime image")
        text = compose.read_text()
        assert "8388" in text, "docker-compose.yml must use port 8388 for ComfyUI"
        assert "8188" not in text, "Port 8188 is wrong — ComfyUI maps to 8388"

    def test_image_gen_default_8388(self):
        source = Path(__file__).resolve().parent.parent / "app" / "image_gen.py"
        text = source.read_text()
        assert "8388" in text, "image_gen.py default should be 8388"

    def test_seed_memories_default_8388(self):
        source = Path(__file__).resolve().parent.parent / "seed_memories.py"
        text = source.read_text()
        assert "8388" in text, "seed_memories.py default should be 8388"
