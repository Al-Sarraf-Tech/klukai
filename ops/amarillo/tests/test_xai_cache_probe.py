"""The cache probe sends real turns and puts the changing note last."""
import importlib.util
from pathlib import Path


def _probe():
    path = Path(__file__).resolve().parents[1] / "scripts" / "klukai-xai-cache-probe.py"
    spec = importlib.util.spec_from_file_location("klukai_xai_cache_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_turn_three_reuses_the_earlier_messages():
    probe = _probe()
    first = probe.messages([("user", "Name one color.")], "turn 1 note")
    third = probe.messages(
        [
            ("user", "Name one color."),
            ("assistant", "Blue."),
            ("user", "Name one city."),
            ("assistant", "Lisbon."),
            ("user", "Name one river."),
        ],
        "turn 3 note",
    )
    assert third[:5] == [
        {"role": "system", "content": probe.PERSONA},
        {"role": "user", "content": "Name one color."},
        {"role": "assistant", "content": "Blue."},
        {"role": "user", "content": "Name one city."},
        {"role": "assistant", "content": "Lisbon."},
    ]
    assert third[-1] == {"role": "system", "content": "turn 3 note"}
    assert first[0] == third[0]
    assert first[1] == third[1]
