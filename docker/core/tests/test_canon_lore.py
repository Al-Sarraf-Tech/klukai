"""Tests for canon-faithful additions: Aphelion arc + GFL2 quirks + system
prompt wiring.

These tests verify that the canonical Klukai material added to
config/personality.yaml is loaded, exposed via the prompt builders, and
actually reaches the assembled system prompt at the right affection gates.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.personality.state_blocks import (
    build_canon_arcs_block,
    build_quirks_block,
)
from app.personality.system_prompt import assemble_system_prompt


@pytest.fixture(scope="module")
def personality() -> dict:
    """Load the actual production personality.yaml — no fixtures, real data."""
    paths = [
        Path("/config/personality.yaml"),
        Path(__file__).resolve().parents[2] / "config" / "personality.yaml",
        Path(__file__).resolve().parents[3] / "config" / "personality.yaml",
    ]
    for p in paths:
        if p.exists():
            return yaml.safe_load(p.read_text())
    pytest.skip("personality.yaml not found in any expected location")


class TestPersonalityYAMLHasCanon:
    def test_canonical_arcs_section_present(self, personality):
        assert "canonical_arcs" in personality
        assert "aphelion" in personality["canonical_arcs"]
        assert "ten_year_silence" in personality["canonical_arcs"]

    def test_aphelion_arc_has_canon_keywords(self, personality):
        aphelion = personality["canonical_arcs"]["aphelion"]
        # Setup mentions Slovakia + Yellow Zone (canon location)
        assert "Slovakia" in aphelion.get("where", "")
        assert "Yellow Zone" in aphelion.get("where", "")
        # Inciting mentions Dmitry's URNC platoon
        assert "Dmitry" in aphelion.get("inciting", "")
        assert "URNC" in aphelion.get("inciting", "")
        # Climax mentions Bluesphere + "I'm here" + Groza
        assert "Bluesphere" in aphelion.get("climax", "")
        assert "I'm here" in aphelion.get("climax", "")
        assert "Groza" in aphelion.get("climax", "")
        assert "ten years" in aphelion.get("climax", "").lower()

    def test_ten_year_silence_arc_canon(self, personality):
        silence = personality["canonical_arcs"]["ten_year_silence"]
        assert "Mephisto" in silence.get("what", "")
        assert "2065" in silence.get("what", "")
        assert "2074" in silence.get("when", "")

    def test_identity_has_canon_quirks(self, personality):
        identity = personality["identity"]
        # Cat-ear hat is canon — touch animation in GFL2
        assert "cat_ear_hat" in identity
        assert "cat" in identity["cat_ear_hat"].lower()
        # Stubborn fee settlements — canon dignity-keeping habit
        assert "stubborn_fee_settlements" in identity
        assert "spreadsheet" in identity["stubborn_fee_settlements"].lower()
        # Vepley nemesis running gag — canon
        assert "vepley_nemesis" in identity
        assert "Vepley" in identity["vepley_nemesis"]
        # Bluesphere trophy moment — canon climax
        assert "bluesphere_trophy" in identity
        assert "Bluesphere" in identity["bluesphere_trophy"]
        # Perfectionist training — canon TV Tropes
        assert "perfectionist_training" in identity
        # Gift from every mission — canon
        assert "gift_from_every_mission" in identity
        # Klukadile plushie — canon
        assert "klukadile" in identity


class TestCanonArcsBlockGating:
    def test_below_level_2_no_arcs(self, personality):
        """Strangers don't get her personal story."""
        for level in (0, 1):
            block = build_canon_arcs_block(personality, level)
            assert block == ""

    def test_level_2_gets_aphelion(self, personality):
        block = build_canon_arcs_block(personality, 2)
        assert "APHELION" in block
        assert "Slovakia" in block
        assert "Dmitry" in block

    def test_level_3_gets_ten_year_silence(self, personality):
        block = build_canon_arcs_block(personality, 3)
        assert "TEN-YEAR SILENCE" in block
        assert "Mephisto" in block

    def test_level_2_does_not_get_ten_year_silence(self, personality):
        """At professional respect, she doesn't pour out 10-year heartbreak."""
        block = build_canon_arcs_block(personality, 2)
        assert "TEN-YEAR SILENCE" not in block

    def test_missing_canonical_arcs_returns_empty(self):
        block = build_canon_arcs_block({}, 9)
        assert block == ""


class TestQuirksBlockGating:
    def test_below_level_3_no_quirks(self, personality):
        for level in (0, 1, 2):
            block = build_quirks_block(personality, level)
            assert block == ""

    def test_level_3_surfaces_quirks(self, personality):
        block = build_quirks_block(personality, 3)
        assert "cat ears" in block.lower()
        assert "Vepley" in block
        assert "Klukadile" in block

    def test_quirks_block_does_not_dump_full_descriptions(self, personality):
        """Quirks should be first-sentence only — prompt budget hygiene.
        The full multi-line description should NOT appear verbatim."""
        block = build_quirks_block(personality, 9)
        # 'WORRIES' is from hidden_softness (multi-sentence), shouldn't leak in
        # the abbreviated quirks block
        assert "WORRIES" not in block

    def test_missing_identity_returns_empty(self):
        block = build_quirks_block({"identity": {}}, 9)
        assert block == ""


class TestSystemPromptCanonIntegration:
    def test_high_affection_prompt_contains_aphelion(self):
        """At affection level 9, Klukai should have full canon access."""
        prompt = assemble_system_prompt(
            affection_level=9, affection_score=1000, mood="composed",
        )
        assert "APHELION" in prompt
        assert "Slovakia" in prompt
        assert "Bluesphere" in prompt
        assert "TEN-YEAR SILENCE" in prompt
        assert "Mephisto" in prompt

    def test_high_affection_prompt_contains_quirks(self):
        prompt = assemble_system_prompt(
            affection_level=9, affection_score=1000, mood="composed",
        )
        assert "Vepley" in prompt
        assert "cat ears" in prompt.lower()
        assert "Klukadile" in prompt

    def test_level_0_prompt_no_canon_arcs(self):
        """Stranger Commander gets no canon arc dump."""
        prompt = assemble_system_prompt(
            affection_level=0, affection_score=0, mood="composed",
        )
        assert "APHELION" not in prompt
        assert "TEN-YEAR SILENCE" not in prompt

    def test_level_2_has_aphelion_but_not_silence(self):
        prompt = assemble_system_prompt(
            affection_level=2, affection_score=100, mood="composed",
        )
        assert "APHELION" in prompt
        assert "TEN-YEAR SILENCE" not in prompt

    def test_prompt_has_canon_squad_relationships(self):
        """Mechty/Belka/Andoris references should be in the squad voices block."""
        prompt = assemble_system_prompt(
            affection_level=5, affection_score=400, mood="composed",
        )
        assert "Mechty" in prompt or "G11" in prompt
        # Squad voices block surfaces them somewhere


class TestCanonEnrichments2026:
    """2026-05-28 lore-dossier enrichments — verify the new canon facts are
    present in config AND (where wired) reach the assembled prompt."""

    def test_signature_weapon_skylla(self, personality):
        sig = personality["identity"].get("signature_weapon", "")
        assert "Skylla" in sig
        assert "Crocodile Tears" in sig

    def test_combat_skills_named(self, personality):
        skills = personality["identity"].get("combat_skills", "")
        assert "Pinpoint Detonation" in skills
        assert "Overpowering Corrosion" in skills

    def test_most_gifted_doll_fact(self, personality):
        assert "gifted" in personality["identity"].get("most_gifted_doll", "").lower()

    def test_blood_tear_hkm4_origin(self, personality):
        assert "HKM4" in personality["identity"].get("blood_tear_tattoo", "")

    def test_indigo_oath_costume_present(self, personality):
        costumes = personality["costumes"]
        assert "indigo_oath" in costumes
        blob = (costumes["indigo_oath"]["type"] + costumes["indigo_oath"]["description"]).lower()
        assert "wedding" in blob or "bridal" in blob

    def test_skylla_in_equipment_weapons(self, personality):
        assert any("Skylla" in w for w in personality["equipment"]["weapons"])

    def test_belka_va_and_class(self, personality):
        belka = personality["relationships"]["belka"]
        assert belka.get("game_class") == "Vanguard"
        assert "Yamamoto" in belka.get("voice_actress", "")

    def test_quirks_block_surfaces_new_facts(self, personality):
        block = build_quirks_block(personality, 3)
        assert "Skylla" in block or "Crocodile Tears" in block
        assert "most-gifted" in block.lower()

    def test_signature_weapon_reaches_high_affection_prompt(self):
        prompt = assemble_system_prompt(
            affection_level=9, affection_score=1000, mood="composed",
        )
        assert "Skylla" in prompt or "Crocodile Tears" in prompt
