"""Tests for personality.squad and personality.loader — pushes both to 100%."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

import pytest

from app.personality import loader
from app.personality.squad import (
    build_squad_interaction_hint,
    build_squad_voices_block,
)


class TestSquadVoicesBlock:
    def test_no_relationships_returns_empty(self):
        assert build_squad_voices_block({}) == ""

    def test_unknown_relationships_returns_empty(self):
        # Relationships exist but none match VOICE_PROFILES
        assert build_squad_voices_block({"relationships": {"random_char": {}}}) == ""

    def test_known_squad_member_included(self):
        out = build_squad_voices_block({"relationships": {"mechty": {}}})
        assert "Mechty" in out
        assert "sleepy" in out.lower()

    def test_multiple_squad_members_each_listed(self):
        out = build_squad_voices_block({
            "relationships": {"mechty": {}, "belka": {}, "andoris": {}}
        })
        assert "Mechty" in out
        assert "Belka" in out
        assert "Andoris" in out


class TestSquadInteractionHint:
    def test_none_target_returns_empty(self):
        assert build_squad_interaction_hint(None) == ""

    def test_empty_string_returns_empty(self):
        assert build_squad_interaction_hint("") == ""

    def test_includes_member_name(self):
        out = build_squad_interaction_hint("Belka")
        assert "Belka" in out
        assert "SQUAD INTERACTION" in out


class TestLoader:
    @pytest.fixture(autouse=True)
    def _reset_loader_state(self):
        """Reset module-level cache before each test; never load the real config."""
        loader._PERSONALITY = None
        loader._PERSONALITY_MTIME = 0
        loader._PERSONALITY_PATH = ""
        yield
        loader._PERSONALITY = None
        loader._PERSONALITY_MTIME = 0
        loader._PERSONALITY_PATH = ""

    def test_get_affection_level_config_finds_existing(self):
        p = {"affection": {"levels": [{"index": 3, "name": "Devoted"}]}}
        result = loader.get_affection_level_config(p, 3)
        assert result["name"] == "Devoted"

    def test_get_affection_level_config_missing_returns_first(self):
        p = {"affection": {"levels": [{"index": 0, "name": "Cold"}, {"index": 1}]}}
        result = loader.get_affection_level_config(p, 99)
        assert result["name"] == "Cold"

    def test_get_affection_level_config_no_levels_returns_empty(self):
        result = loader.get_affection_level_config({}, 0)
        assert result == {}

    def test_get_speech_patterns_level_0(self):
        p = {"speech_patterns": {"level_0_cold": {"name": "Cold"}}}
        result = loader.get_speech_patterns(p, 0)
        assert result["name"] == "Cold"

    def test_get_speech_patterns_levels_5_through_9_use_bonded(self):
        # Per feedback_speech_routing_bug.md: levels 5-9 MUST use bonded
        p = {"speech_patterns": {"level_4_bonded": {"name": "Bonded"}}}
        for level in range(4, 10):
            result = loader.get_speech_patterns(p, level)
            assert result["name"] == "Bonded", f"level {level} should use bonded"

    def test_get_speech_patterns_each_low_level_distinct(self):
        # Levels 0-3 have distinct keys
        p = {"speech_patterns": {
            "level_0_cold": {"name": "lvl_0"},
            "level_1_professional": {"name": "lvl_1"},
            "level_2_trusted": {"name": "lvl_2"},
            "level_3_devoted": {"name": "lvl_3"},
        }}
        for i in range(4):
            result = loader.get_speech_patterns(p, i)
            assert result["name"] == f"lvl_{i}"

    def test_get_speech_patterns_missing_returns_empty(self):
        result = loader.get_speech_patterns({}, 0)
        assert result == {}

    def test_load_personality_uses_path_arg(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("user_title: TestCommander\n")
            tmp_path = f.name
        try:
            p = loader.load_personality(tmp_path)
            assert p["user_title"] == "TestCommander"
        finally:
            os.unlink(tmp_path)

    def test_load_personality_returns_cached_on_repeat(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("key: original\n")
            tmp_path = f.name
        try:
            first = loader.load_personality(tmp_path)
            second = loader.load_personality(tmp_path)
            # Same object reference = cached
            assert first is second
        finally:
            os.unlink(tmp_path)

    def test_load_personality_missing_file_handled(self):
        with pytest.raises(FileNotFoundError):
            loader.load_personality("/nonexistent/path/to/personality.yaml")

    def test_load_personality_uses_env_default(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('from_env: "loaded"\n')
            tmp_path = f.name
        try:
            with patch.dict(os.environ, {"PERSONALITY_PATH": tmp_path}):
                p = loader.load_personality(None)
                assert p["from_env"] == "loaded"
        finally:
            os.unlink(tmp_path)

    def test_reload_personality_forces_fresh(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("v: 1\n")
            tmp_path = f.name
        try:
            first = loader.load_personality(tmp_path)
            # Rewrite + bump mtime by 1s so the auto-reload triggers
            with open(tmp_path, "w") as f:
                f.write("v: 2\n")
            os.utime(tmp_path, (os.path.getmtime(tmp_path) + 1, os.path.getmtime(tmp_path) + 1))
            refreshed = loader.reload_personality(tmp_path)
            assert refreshed["v"] == 2
            assert first["v"] == 1  # original dict unchanged
        finally:
            os.unlink(tmp_path)
