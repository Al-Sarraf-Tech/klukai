"""Smoke tests for companion-core: imports, pure functions, and model validation.

These tests run without any external services (no DB, Redis, LM Studio, etc.).
Tests requiring psycopg/redis are skipped when those packages are not installed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# Ensure the app package is importable from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Import tests ────────────────────────────────────────────────────────────


class TestImports:
    """Verify modules that have no heavy native deps import without error."""

    def test_import_models(self):
        from app.models import SessionState, LLMConfig, Mood, Role

    def test_import_personality(self):
        from app.personality import (
            build_character_preamble,
            build_character_rules,
            build_context_block,
            build_memory_block,
            build_pace_block,
            build_relationship_block,
        )

    def test_import_llm_router(self):
        from app.llm_router import LLMRouter, LLMConfig

    def test_import_image_gen(self):
        from app.image_gen import needs_image, is_couple_scene, build_prompt, is_landscape

    def test_import_ws_manager(self):
        from app.ws_manager import WSManager

    def test_import_tool_schemas(self):
        from app.tool_schemas import mcp_to_openai

    def test_import_affection(self):
        pytest.importorskip("psycopg")
        from app.affection import AffectionManager, AffectionState

    def test_import_main(self):
        pytest.importorskip("psycopg")
        from app.main import _fix_narration, _enhance_image_prompt


# ── Personality tests ───────────────────────────────────────────────────────


class TestPersonality:
    """Test personality prompt assembly (pure functions, no I/O)."""

    @pytest.fixture
    def personality_dict(self) -> dict:
        """Minimal personality dict for testing without YAML file."""
        return {
            "user_title": "Commander",
            "affection": {
                "levels": [
                    {"index": 0, "threshold": 0, "name": "Cold Assessment", "prompt_modifier": "Be cold."},
                    {"index": 1, "threshold": 20, "name": "Professional Respect", "prompt_modifier": "Be professional."},
                    {"index": 2, "threshold": 50, "name": "Trusted Ally", "prompt_modifier": "Be warm."},
                    {"index": 3, "threshold": 80, "name": "Devoted Bond", "prompt_modifier": "Be devoted."},
                    {"index": 4, "threshold": 95, "name": "Oath Sealed", "prompt_modifier": "Be bonded."},
                ],
            },
            "speech_patterns": {},
            "expressive_tokens": {},
            "japanese_phrases": {},
        }

    def test_build_character_preamble_returns_string(self, personality_dict):
        from app.personality import build_character_preamble

        result = build_character_preamble(personality_dict, affection_level=0)
        assert isinstance(result, str)
        assert len(result) > 50

    def test_build_character_preamble_contains_commander(self, personality_dict):
        from app.personality import build_character_preamble

        result = build_character_preamble(personality_dict, affection_level=0)
        assert "Commander" in result

    def test_build_character_preamble_contains_klukai(self, personality_dict):
        from app.personality import build_character_preamble

        result = build_character_preamble(personality_dict, affection_level=0)
        assert "Klukai" in result

    def test_build_character_rules_contains_absolute_rules(self):
        from app.personality import build_character_rules

        result = build_character_rules()
        assert isinstance(result, str)
        assert "ABSOLUTE RULES" in result

    def test_build_character_rules_contains_narration_rules(self):
        from app.personality import build_character_rules

        result = build_character_rules()
        assert "NARRATION RULES" in result

    def test_build_pace_block_short_message(self):
        from app.personality import build_pace_block

        result = build_pace_block(last_msg_length=10)
        assert "short" in result.lower()

    def test_build_pace_block_empty(self):
        from app.personality import build_pace_block

        result = build_pace_block(last_msg_length=0)
        assert result == ""

    def test_build_memory_block_empty(self):
        from app.personality import build_memory_block

        result = build_memory_block([])
        assert result == ""

    def test_build_memory_block_with_data(self):
        from app.personality import build_memory_block

        result = build_memory_block(["The Commander likes coffee", "Discussed tactics"])
        assert "OPERATIONAL RECORDS" in result
        assert "coffee" in result

    def test_build_relationship_block_empty(self):
        from app.personality import build_relationship_block

        result = build_relationship_block({})
        assert result == ""

    def test_build_relationship_block_with_facts(self):
        from app.personality import build_relationship_block

        result = build_relationship_block({"name": "Jamal", "hobby": "coding"})
        assert "COMMANDER DOSSIER" in result
        assert "Jamal" in result

    def test_build_context_block_returns_string(self):
        from app.personality import build_context_block

        result = build_context_block(mood="composed", affection_level=0)
        assert isinstance(result, str)
        assert "OPERATIONAL CONTEXT" in result

    def test_build_affection_block(self, personality_dict):
        from app.personality import build_affection_block

        result = build_affection_block(
            affection_score=50,
            affection_level=2,
            affection_level_name="Trusted Ally",
            p=personality_dict,
        )
        assert "AFFECTION STATE" in result
        assert "Trusted Ally" in result


# ── Affection level computation ─────────────────────────────────────────────


class TestAffectionComputeLevel:
    """Test AffectionManager._compute_level (pure, no I/O).

    Skipped when psycopg is not installed (the module imports db.py).
    """

    @pytest.fixture
    def manager(self):
        psycopg = pytest.importorskip("psycopg")
        from app.affection import AffectionManager

        mgr = AffectionManager()
        mgr._levels = [
            {"index": 0, "threshold": 0, "name": "Cold Assessment"},
            {"index": 1, "threshold": 20, "name": "Professional Respect"},
            {"index": 2, "threshold": 50, "name": "Trusted Ally"},
            {"index": 3, "threshold": 80, "name": "Devoted Bond"},
            {"index": 4, "threshold": 95, "name": "Oath Sealed"},
        ]
        return mgr

    def test_score_0_is_cold(self, manager):
        level, name = manager._compute_level(0)
        assert level == 0
        assert name == "Cold Assessment"

    def test_score_25_is_professional(self, manager):
        level, name = manager._compute_level(25)
        assert level == 1
        assert name == "Professional Respect"

    def test_score_50_is_trusted(self, manager):
        level, name = manager._compute_level(50)
        assert level == 2
        assert name == "Trusted Ally"

    def test_score_80_is_devoted(self, manager):
        level, name = manager._compute_level(80)
        assert level == 3
        assert name == "Devoted Bond"

    def test_score_100_is_oath(self, manager):
        level, name = manager._compute_level(100)
        assert level == 4
        assert name == "Oath Sealed"

    def test_score_19_still_cold(self, manager):
        level, name = manager._compute_level(19)
        assert level == 0
        assert name == "Cold Assessment"

    def test_score_boundary_20(self, manager):
        level, name = manager._compute_level(20)
        assert level == 1


# ── Session state model ─────────────────────────────────────────────────────


class TestSessionState:
    """Test SessionState model from models.py."""

    def test_default_mood_is_composed(self):
        from app.models import SessionState

        s = SessionState(conversation_id="test-123")
        assert s.mood == "composed"
        assert s.turn_count == 0
        assert s.turns == []

    def test_serialization_roundtrip(self):
        from app.models import SessionState

        s = SessionState(conversation_id="test-456", mood="tender", turn_count=5)
        json_str = s.model_dump_json()
        restored = SessionState.model_validate_json(json_str)
        assert restored.conversation_id == "test-456"
        assert restored.mood == "tender"
        assert restored.turn_count == 5


# ── Narration fix (inlined — avoids psycopg dep from main.py import) ────────


def _fix_narration(text: str) -> str:
    """Inline copy of main._fix_narration for testing without psycopg."""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<\|think\|>.*?<\|/think\|>', '', text, flags=re.DOTALL)
    text = re.sub(r'\(You ([a-z])', lambda m: f'(I {m.group(1)}', text)
    text = re.sub(r'\(Your ', '(My ', text)
    text = re.sub(r'\(your ', '(my ', text)
    text = re.sub(r'\([^)]*(?:your face|your eyes|your expression|your mouth|crosses your|touches your)[^)]*\)', '', text)
    while text.endswith('|'):
        text = text[:-1]
    text = text.rstrip(' ')
    text = re.sub(r'  +', ' ', text)
    return text


class TestFixNarration:
    """Test _fix_narration logic (inlined to avoid psycopg import chain)."""

    def test_strips_think_tags(self):
        text = "<think>internal reasoning here</think>Hello, Commander."
        result = _fix_narration(text)
        assert "think" not in result.lower()
        assert "Hello, Commander." in result

    def test_strips_think_tags_alternate_format(self):
        text = "<|think|>reasoning block<|/think|>Understood."
        result = _fix_narration(text)
        assert "think" not in result.lower()
        assert "Understood." in result

    def test_converts_you_to_i(self):
        text = "(You pause and look away)"
        result = _fix_narration(text)
        assert "(I pause" in result

    def test_converts_your_to_my(self):
        text = "(Your expression softens)"
        result = _fix_narration(text)
        assert "(My expression" in result

    def test_strips_commander_narration(self):
        text = "Hello. (your eyes widen) How are you?"
        result = _fix_narration(text)
        assert "your eyes" not in result

    def test_strips_trailing_pipes(self):
        text = "Response text|||"
        result = _fix_narration(text)
        assert result == "Response text"

    def test_preserves_normal_text(self):
        text = "Understood, Commander. (I nod) Moving out."
        result = _fix_narration(text)
        assert result == text


# ── Image prompt enhancement (inlined) ──────────────────────────────────────


def _enhance_image_prompt(user_request: str, couple: bool = False) -> str:
    """Inline copy of main._enhance_image_prompt for testing without psycopg."""
    lower = user_request.lower()
    tags = []

    SCENE_MAP = {
        "sunset": "sunset, orange sky, golden hour lighting",
        "night": "night, moonlight, dark sky, stars",
        "rain": "rain, wet, umbrella, overcast",
        "snow": "snow, winter, cold breath, scarf",
        "beach": "beach, ocean, sand, swimsuit, summer",
        "cafe": "cafe, table, coffee cup, indoor, cozy",
        "battle": "battlefield, smoke, debris, action pose",
        "motorcycle": "motorcycle, riding, wind, speed lines, road",
        "bed": "bedroom, bed, pillows, soft lighting, intimate",
        "rooftop": "rooftop, city skyline, wind, evening",
        "garden": "garden, flowers, natural lighting, peaceful",
        "office": "office, desk, computer, indoor lighting",
        "forest": "forest, trees, nature, sunlight through leaves",
        "city": "city, urban, street, buildings, neon",
    }
    for keyword, scene_tags in SCENE_MAP.items():
        if keyword in lower:
            tags.append(scene_tags)

    MOOD_MAP = {
        "kiss": "kiss, eyes closed, romantic",
        "hug": "hug, embrace, close, warm",
        "cuddle": "cuddling, lying down, comfortable, close",
        "hold": "holding hands, close, side by side",
        "smile": "smile, happy, cheerful",
        "blush": "blush, embarrassed, looking away",
        "cry": "tears, emotional, sad",
        "fight": "fighting stance, action, dynamic pose",
        "sleep": "sleeping, peaceful, eyes closed",
        "eat": "eating, food, table",
        "cook": "cooking, kitchen, apron",
        "read": "reading, book, sitting, quiet",
    }
    for keyword, mood_tags in MOOD_MAP.items():
        if keyword in lower:
            tags.append(mood_tags)

    if not tags:
        tags.append("standing, looking at viewer, detailed background")

    return ", ".join(tags)


class TestEnhanceImagePrompt:
    """Test _enhance_image_prompt logic (inlined to avoid psycopg import chain)."""

    def test_returns_comma_separated_tags(self):
        result = _enhance_image_prompt("show me a sunset scene")
        assert isinstance(result, str)
        assert "," in result
        assert "sunset" in result

    def test_multiple_keywords(self):
        result = _enhance_image_prompt("draw us kissing at the beach")
        assert "kiss" in result
        assert "beach" in result

    def test_no_match_gets_default(self):
        result = _enhance_image_prompt("just something generic")
        assert "standing" in result
        assert "looking at viewer" in result

    def test_couple_flag(self):
        result = _enhance_image_prompt("a night scene", couple=True)
        assert "night" in result


# ── Image generation helpers ─────────────────────────────────────────────────


class TestImageGenHelpers:
    """Test pure functions from image_gen.py."""

    def test_needs_image_positive(self):
        from app.image_gen import needs_image

        assert needs_image("show me a picture of you")
        assert needs_image("draw us together")
        assert needs_image("generate an image of a sunset")

    def test_needs_image_negative(self):
        from app.image_gen import needs_image

        assert not needs_image("how are you today")
        assert not needs_image("tell me about your squad")

    def test_is_couple_scene_positive(self):
        from app.image_gen import is_couple_scene

        assert is_couple_scene("show us together")
        assert is_couple_scene("imagine us cuddling")
        assert is_couple_scene("draw a picture of both of us")

    def test_is_couple_scene_negative(self):
        from app.image_gen import is_couple_scene

        assert not is_couple_scene("draw klukai standing alone")
        assert not is_couple_scene("show the rifle")

    def test_is_landscape(self):
        from app.image_gen import is_landscape

        assert is_landscape("show me the sunset landscape")
        assert is_landscape("draw the battlefield")
        assert not is_landscape("draw a portrait of you")

    def test_build_prompt_includes_quality(self):
        from app.image_gen import build_prompt

        result = build_prompt("sunset, orange sky")
        assert "masterpiece" in result
        assert "Klukai" in result


# ── LLM config model ────────────────────────────────────────────────────────


class TestLLMConfig:
    """Test LLMConfig model."""

    def test_defaults(self):
        from app.models import LLMConfig

        config = LLMConfig(provider="lmstudio", model="test-model")
        assert config.temperature == 0.8
        assert config.max_tokens == 2048

    def test_anthropic_config(self):
        from app.models import LLMConfig

        config = LLMConfig(provider="anthropic", model="claude-sonnet", temperature=0.7)
        assert config.provider == "anthropic"
        assert config.temperature == 0.7


# ── Tool schema conversion ──────────────────────────────────────────────────


class TestToolSchemas:
    """Test MCP-to-OpenAI schema conversion."""

    def test_mcp_to_openai_basic(self):
        from app.tool_schemas import mcp_to_openai

        mcp_tool = {
            "name": "web_search",
            "description": "Search the web",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        }
        result = mcp_to_openai(mcp_tool)
        assert result["type"] == "function"
        assert result["function"]["name"] == "web_search"
        assert "query" in result["function"]["parameters"]["properties"]

    def test_mcp_to_openai_missing_schema(self):
        from app.tool_schemas import mcp_to_openai

        mcp_tool = {"name": "simple_tool", "description": "A simple tool"}
        result = mcp_to_openai(mcp_tool)
        assert result["function"]["name"] == "simple_tool"
        assert result["function"]["parameters"]["type"] == "object"
