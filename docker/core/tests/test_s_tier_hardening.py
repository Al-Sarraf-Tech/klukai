"""S-tier hardening tests: security, Pydantic validation, per-user proactive,
atomic transactions, subsystem health, episode dual-write."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

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
                    assert "CompanionMemory" not in names, (
                        "CompanionMemory import found — this class doesn't exist"
                    )

    def test_recall_uses_recall_episodes_not_search_episodic(self):
        """search_episodic doesn't exist — must use recall_episodes."""
        source = Path(__file__).resolve().parent.parent / "app" / "agent_loop.py"
        text = source.read_text()
        assert "search_episodic" not in text, (
            "search_episodic doesn't exist on MemoryManager"
        )
        assert "recall_episodes" in text, "Should use recall_episodes"

    def test_recall_uses_sql_like_pattern(self):
        """Data API uses SQL LIKE (%), not glob (*)."""
        source = Path(__file__).resolve().parent.parent / "app" / "agent_loop.py"
        text = source.read_text()
        # Should NOT have glob patterns in the recall function
        # The function builds pattern with % not *
        recall_func = text[
            text.index("_builtin_recall_memory") : text.index("_builtin_get_time")
        ]
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
        users = [
            line.split()[1] for line in text.splitlines() if line.startswith("USER ")
        ]
        assert users, "The final runtime must explicitly select a user"
        assert users[-1].split(":")[0] not in {"root", "0"}
        # The release suite additionally checks the actual image UID. A
        # distroless runtime has no shell/useradd and accepts numeric IDs.

    def test_costume_endpoints_require_auth(self):
        source = Path(__file__).resolve().parent.parent / "app" / "routes_extras.py"
        text = source.read_text()
        handlers = {
            node.name: ast.get_source_segment(text, node)
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name in {"api_get_costume", "api_set_costume"}
        }
        assert set(handlers) == {"api_get_costume", "api_set_costume"}
        for name, handler in handlers.items():
            assert "await _get_user_id(request)" in handler, name
            assert "status_code=401" in handler, name


# ═══════════════════════════════════════════════════════════════════════════
# 5. Atomic Transaction for store_message
# ═══════════════════════════════════════════════════════════════════════════


class TestAtomicTransaction:
    """Verify store_message uses atomic transaction."""

    def test_store_message_uses_commit(self):
        source = (
            Path(__file__).resolve().parent.parent / "app" / "conversation_store.py"
        )
        text = source.read_text()
        # Inspect the persistence owner, independent of the helpers re-export
        func_start = text.index("async def store_message")
        func_text = text[func_start:]
        assert "await conn.commit()" in func_text, (
            "Must explicitly commit for atomicity"
        )
        assert "get_conn()" in func_text, (
            "Must use get_conn (manual commit) not get_conn_autocommit"
        )


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
        assert "companion_episodes" in func_text, (
            "Must write to companion_episodes DB table"
        )
        assert "QDRANT_URL" in func_text or "COLLECTION_NAME" in func_text, (
            "Must write to Qdrant"
        )
        assert "get_conn_autocommit" in func_text, (
            "Must use DB connection for PostgreSQL write"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 7. Subsystem Health Endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestSubsystemHealth:
    """Verify subsystem health endpoint exists and checks all services."""

    def test_subsystem_health_endpoint_exists(self):
        source = Path(__file__).resolve().parent.parent / "app" / "routes_health.py"
        text = source.read_text()
        assert "/api/health/subsystems" in text

    def test_checks_all_seven_subsystems(self):
        source = Path(__file__).resolve().parent.parent / "app" / "routes_health.py"
        text = source.read_text()
        # Find the subsystem_health function
        func_text = text
        assert "results.update(await _image_subsystems())" in func_text
        for subsystem in [
            "database",
            "redis",
            "qdrant",
            "lm_studio",
            "comfyui",
            "embeddings",
            "voice",
        ]:
            assert subsystem in func_text, f"Must check {subsystem} subsystem"

    def test_subsystem_health_requires_auth(self):
        source = Path(__file__).resolve().parent.parent / "app" / "routes_health.py"
        text = source.read_text()
        func_text = text
        assert "await resolve_user(request)" in func_text, (
            "Subsystem health must require authentication"
        )
        assert "status_code=401" in func_text
        registry = (source.parent / "routes.py").read_text()
        assert "lambda request: _get_user_id(request)" in registry


# ═══════════════════════════════════════════════════════════════════════════
# 8. ComfyUI authenticated facade
# ═══════════════════════════════════════════════════════════════════════════


class TestComfyUIFacade:
    """Verify Klukai cannot bypass the gateway's exclusive GPU lease."""

    def test_docker_compose_uses_gateway_facade(self):
        compose = (
            Path(__file__).resolve().parent.parent.parent.parent / "docker-compose.yml"
        )
        if not compose.exists():
            import pytest

            pytest.skip("docker-compose.yml not shipped into runtime image")
        text = compose.read_text()
        assert "http://100.107.121.5:1234/api/v1/comfy" in text
        assert "100.107.121.5:8388" not in text

    def test_image_gen_default_uses_gateway_facade(self):
        source = Path(__file__).resolve().parent.parent / "app" / "image_gen.py"
        text = source.read_text()
        assert "http://100.107.121.5:1234/api/v1/comfy" in text
        assert "GPU_LEASE_HEADER" not in text  # use the central header helper

    def test_seed_memories_reuses_protected_image_path(self):
        source = Path(__file__).resolve().parent.parent / "seed_memories.py"
        text = source.read_text()
        assert "from app.image_gen import" in text
        assert '"/prompt"' not in text
        assert '"/free"' not in text

    def test_regen_images_reuses_protected_image_path(self):
        source = (
            Path(__file__).resolve().parent.parent.parent.parent
            / "scripts"
            / "regen_images.py"
        )
        if not source.exists():
            import pytest

            pytest.skip("scripts/regen_images.py not shipped into runtime image")
        text = source.read_text()
        assert "from app.image_gen import generate_image" in text
        assert '"/prompt"' not in text
        assert '"/history' not in text


class TestTailscaleBackendConfig:
    """GPU backend defaults use Tailscale MagicDNS and remain overridable."""

    def test_compose_uses_magicdns_defaults(self):
        compose = (
            Path(__file__).resolve().parent.parent.parent.parent / "docker-compose.yml"
        )
        if not compose.exists():
            import pytest

            pytest.skip("docker-compose.yml not shipped into runtime image")
        text = compose.read_text()
        assert "${LM_STUDIO_URL:-http://100.107.121.5:1234}" in text
        assert "${VOICE_URL:-http://100.107.121.5:8301}" in text
        assert "${COMFYUI_URL:-http://100.107.121.5:1234/api/v1/comfy}" in text
        assert "192.168.50.2" not in text
