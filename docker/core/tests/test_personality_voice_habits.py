"""Voice habits the persona must not teach, and the two code changes behind them."""
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml

from app.chat_prompt import _recent_openings
from app.personality.moods import CATEGORY_BLEED_RULES, MOOD_SPECIFIC_BLEED
from app.personality.rules import build_character_rules
from app.proactive.events import _select_lore_message

CONFIG = yaml.safe_load(
    (Path(__file__).resolve().parents[3] / "config/personality.yaml").read_text()
)


def test_bonded_examples_do_not_open_on_a_pause_or_a_stage_direction():
    for line in CONFIG["speech_patterns"]["level_4_bonded"]["examples"]:
        assert not line.startswith(("...", "("))


def test_prompt_has_no_copyable_love_answer():
    assert "...I know" not in build_character_rules()
    assert "Say it again" not in build_character_rules()


def test_moods_do_not_ask_for_ellipses():
    assert "ellipses" not in CATEGORY_BLEED_RULES["romantic"]
    assert "ellipses" not in MOOD_SPECIFIC_BLEED["tender"]


def test_recent_openings_keeps_only_her_last_replies():
    turns = [
        {"role": "assistant", "content": "...I know. (I lean in) Stay."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "Already moved your afternoon, Commander."},
    ]
    assert _recent_openings(turns) == ["...I know. (I lean", "Already moved your afternoon,"]
    assert _recent_openings(turns, keep=1) == ["Already moved your afternoon,"]
    assert _recent_openings([]) == []


def test_lore_lines_respect_their_months():
    events = {
        "summer": {"months": [6], "weight": 10, "messages": ["heat"]},
        "any": {"weight": 10, "messages": ["rain"]},
    }
    with patch("app.proactive.events.now_local", return_value=datetime(2026, 10, 31, 12)):
        assert {_select_lore_message(events, {}, 9) for _ in range(30)} == {"rain"}
    with patch("app.proactive.events.now_local", return_value=datetime(2026, 6, 1, 12)):
        assert {_select_lore_message(events, {}, 9) for _ in range(60)} == {"heat", "rain"}
