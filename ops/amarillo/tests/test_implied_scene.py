"""fal's implied-intimacy rewrite (owner 2026-10-24)."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import implied_scene as scene  # noqa: E402

# The shape of a refused Her POV prompt (2026-10-24): fully clothed, yet refused.
REFUSED = (
    "Create one polished anime illustration of the described scene.\n"
    "Scene and appearance: Klukai; couple; one man; one woman; hetero; male; shirtless; "
    "bare chest; silver hair; detailed skin; perfect anatomy; medium breasts; kigurumi; "
    "fully zipped; one woman; 1other; kiss; fingers in hair; parted lips; flushed face; "
    "trembling; half-lidded eyes; intimate; dim lighting; night.\n"
    "Avoid: extra fingers."
)
TRIGGERS = ("nude", "nudity", "naked", "sexual", "explicit", "nsfw", "lingerie", "breast",
            "shirtless", "bare chest", "hetero", "seductive", "flushed", "parted lips")


@pytest.mark.parametrize("rewrite", [scene.implied, scene.plain])
def test_no_filter_trigger_survives_or_is_added(rewrite):
    out = rewrite(REFUSED).lower()
    for word in TRIGGERS:
        assert word not in out, word


def test_implied_keeps_the_romance_and_tenderness():
    line = scene.implied(REFUSED).split("\n")[1]
    assert "kiss" in line and "kigurumi" in line and "tender" in line
    assert "Commander wears a loose open-collared shirt" in line


def test_plain_dresses_both_and_drops_touch():
    line = scene.plain(REFUSED).split("\n")[1]
    assert "kiss" not in line and "fingers in hair" not in line and "dim lighting" not in line
    assert "Klukai is fully clothed" in line and "casual shirt" in line


def test_undress_in_bed_becomes_a_quilt():
    prompt = "Scene and appearance: one woman; black lace lingerie; lying on bed; seductive pose."
    out = scene.implied(prompt)
    assert "lingerie" not in out and "seductive" not in out
    assert "quilt is pulled up to the shoulders over her" in out


def test_other_lines_and_unstructured_prompts_are_untouched():
    out = scene.implied(REFUSED).split("\n")
    assert out[0] == REFUSED.split("\n")[0] and out[2] == "Avoid: extra fingers."
    assert scene.implied("Klukai reading by the window") == "Klukai reading by the window"


def test_rewrite_is_stable():
    once = scene.implied(REFUSED)
    assert scene.implied(once).split("\n")[1].count("modest and tasteful") == 1


def test_undress_in_a_bath_becomes_a_towel_and_steam():
    prompt = ("Scene and appearance: one woman; onsen; steam; wet skin; towel barely covering; "
              "nude; bare shoulders.")
    out = scene.implied(prompt)
    for word in ("nude", "wet skin", "barely"):
        assert word not in out
    assert "wrapped in a large white towel" in out and "blanket" not in out


CHAT = (
    "draw yourself in the bath\n\nSubject: her in the bath\n\n"
    "Setting: Indoors - a steamy washroom, tiled walls.\n\n"
    "One polished anime illustration of the scene below.\n"
    "Scene and appearance: Klukai; one woman; steam; towel barely covering."
)


@pytest.mark.parametrize("rewrite", [scene.implied, scene.plain])
def test_his_typed_sentence_is_left_out_and_the_setting_kept(rewrite):
    out = rewrite(CHAT)
    assert out.startswith("Setting: Indoors - a steamy washroom")
    assert "draw yourself" not in out and "Subject:" not in out


def test_a_solo_picture_is_never_told_between_adults():
    solo = scene.implied(CHAT)
    assert "modest and tasteful" in solo and "between adults" not in solo
    assert "wrapped in a large white towel" in solo
    assert "between adults" in scene.implied(REFUSED)
