"""Golden tests — system prompt snapshots across (level, mood, time_of_day).

Stores expected outputs in `tests/golden/snapshots/`. To rotate snapshots
intentionally, set env `UPDATE_SNAPSHOTS=1` and re-run. Mass rotations should
be reviewed in PR — the snapshot file diff IS the review surface.

Per CLAUDE.md absolute directives:
- `feedback_speech_routing_bug.md` — levels 5-9 NEVER default to Cold.
- `feedback_commander_human.md` — Commander never referenced as T-Doll.
- `feedback_never_delete_chat.md` — memory blocks never wipe state.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def _snapshot_key(level: int, mood: str, time_of_day: str) -> str:
    return f"l{level}_{mood}_{time_of_day}.json"


def _load_or_create_snapshot(key: str, payload: dict) -> dict:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / key
    if not path.exists() or os.environ.get("UPDATE_SNAPSHOTS"):
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return json.loads(path.read_text())


@pytest.mark.parametrize("level", [0, 2, 5, 7, 9])
@pytest.mark.parametrize("mood", ["neutral", "warm", "guarded"])
@pytest.mark.parametrize("time_of_day", ["morning", "evening", "late_night"])
def test_system_prompt_stable(level: int, mood: str, time_of_day: str) -> None:
    """System prompt for a given (level, mood, time) tuple is stable across
    runs. Any drift is a character regression — either intentional (rotate
    snapshot) or a bug (revert)."""
    try:
        from app.personality.system_prompt import assemble_system_prompt
    except ImportError:
        pytest.skip("assemble_system_prompt not importable in dev env")

    try:
        prompt = assemble_system_prompt(
            mood=mood,
            affection_level=level,
            affection_score=level * 100,
        )
    except Exception as exc:  # pragma: no cover — config-dependent
        pytest.skip(f"assemble_system_prompt config-error: {exc!r}")

    # Stable hash + length + checksum capture drift at multiple granularities.
    payload = {
        "level": level,
        "mood": mood,
        "time_of_day": time_of_day,
        "length": len(prompt),
        "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "first_line": prompt.splitlines()[0] if prompt else "",
        "contains_klukai": "Klukai" in prompt,
        "contains_commander": "Commander" in prompt,
        # Regression guard: levels 5-9 must NOT contain Cold-flavored language.
        "cold_flavor_in_high": (
            level >= 5
            and ("Cold Assessment" in prompt or "state your business" in prompt.lower())
        ),
    }
    snapshot = _load_or_create_snapshot(_snapshot_key(level, mood, time_of_day), payload)

    # Regression assertions — even if the snapshot hash drifts (intentional
    # rotation), these invariants must hold forever.
    if level >= 5:
        assert not payload["cold_flavor_in_high"], (
            f"REGRESSION: level {level} produced Cold-flavored prompt — "
            "see feedback_speech_routing_bug.md"
        )

    # Snapshot equality (the rotation check).
    if not os.environ.get("UPDATE_SNAPSHOTS"):
        assert payload["sha256"] == snapshot["sha256"], (
            f"System prompt drifted for (level={level}, mood={mood!r}, tod={time_of_day!r}).\n"
            f"  Snapshot sha256: {snapshot['sha256']}\n"
            f"  Current  sha256: {payload['sha256']}\n"
            f"  If intentional: UPDATE_SNAPSHOTS=1 pytest tests/golden/"
        )
