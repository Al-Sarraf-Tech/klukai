"""Tests for seed_memories.py configuration: model selection, prompt structure."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestSeedConfig:
    """Verify seed script uses correct models and prompts."""

    @pytest.fixture
    def seed_module_text(self):
        seed_path = Path(__file__).resolve().parent.parent / "seed_memories.py"
        return seed_path.read_text()

    def test_selector_uses_gpt_oss(self, seed_module_text):
        """Selection pass must use gpt-oss-20b for reliable JSON."""
        assert 'SELECTOR_MODEL = "gpt-oss-20b' in seed_module_text

    def test_annotator_uses_dolphin(self, seed_module_text):
        """Annotation pass must use dolphin — gpt-oss-20b leaks chain-of-thought."""
        assert 'ANNOTATOR_MODEL = "cognitivecomputations_dolphin' in seed_module_text
        assert 'ANNOTATOR_MODEL = "gpt-oss' not in seed_module_text

    def test_commander_is_human_in_prompts(self, seed_module_text):
        """Prompts must establish Commander as HUMAN, not T-Doll."""
        assert "Commander is HUMAN" in seed_module_text

    def test_selection_prompt_requests_json(self, seed_module_text):
        """Selection prompt must request JSON output."""
        assert "valid JSON" in seed_module_text

    def test_annotation_prompt_requests_rich_entries(self, seed_module_text):
        """Annotation prompt must request 3-5 sentence journal entries."""
        assert "3-5 sentences" in seed_module_text

    def test_all_categories_present(self, seed_module_text):
        """All 6 memory categories must be defined."""
        categories = [
            "Tactical Operations", "Mission Records", "Squad Moments",
            "The Commander", "Quiet Hours", "Precious Memories",
        ]
        for cat in categories:
            assert cat in seed_module_text, f"Missing category: {cat}"

    def test_image_tags_prompt_describes_scene(self, seed_module_text):
        """Image tag prompt must request scene description, not abstract concepts."""
        assert "WHERE are they" in seed_module_text or "image_tags" in seed_module_text

    def test_retry_logic_present(self, seed_module_text):
        """Seed script must retry failed batches."""
        assert "failed_batches" in seed_module_text or "retry" in seed_module_text.lower()
